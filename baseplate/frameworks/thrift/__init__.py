import sys
import time

from logging import Logger
from typing import Any
from typing import Callable
from typing import Mapping
from typing import Optional

from contextlib import contextmanager

from prometheus_client import Counter
from prometheus_client import Gauge
from prometheus_client import Histogram
from requests.structures import CaseInsensitiveDict
from thrift.protocol.TProtocol import TProtocolBase
from thrift.protocol.TProtocol import TProtocolException
from thrift.Thrift import TApplicationException
from thrift.Thrift import TException
from thrift.Thrift import TProcessor
from thrift.transport.TTransport import TTransportException

from opentelemetry.propagate import extract
from opentelemetry import trace
from opentelemetry.context import attach, detach

from baseplate import Baseplate
from baseplate import RequestContext
from baseplate import TraceInfo
from baseplate.lib.edgecontext import EdgeContextFactory
from baseplate.lib.prometheus_metrics import default_latency_buckets
from baseplate.thrift.ttypes import Error
from baseplate.thrift.ttypes import ErrorCode

PROM_NAMESPACE = "thrift_server"

PROM_LATENCY = Histogram(
    f"{PROM_NAMESPACE}_latency_seconds",
    "Time spent processing requests",
    [
        "thrift_method",
        "thrift_success",
    ],
    buckets=default_latency_buckets,
)
PROM_REQUESTS = Counter(
    f"{PROM_NAMESPACE}_requests_total",
    "Total RPC request count",
    [
        "thrift_method",
        "thrift_success",
        "thrift_exception_type",
        "thrift_baseplate_status",
        "thrift_baseplate_status_code",
    ],
)
PROM_ACTIVE = Gauge(
    f"{PROM_NAMESPACE}_active_requests",
    "The number of in-flight requests being handled by the service",
    ["thrift_method"],
    multiprocess_mode="livesum",
)


class _ContextAwareHandler:
    def __init__(
        self,
        handler: Any,
        context: RequestContext,
        logger: Logger,
        convert_to_baseplate_error: bool,
    ):
        self.handler = handler
        self.context = context
        self.logger = logger
        self.convert_to_baseplate_error = convert_to_baseplate_error
        self.tracer = trace.get_tracer(__name__)

    @contextmanager
    def _set_remote_context(self, servicer_context):
        headers = servicer_context.headers
        if headers:
            str_headers = {}
            for k, v in headers.items():
                str_headers[k.decode()] = v.decode()
            ctx = extract(str_headers)
            token = attach(ctx)
            try:
                yield
            finally:
                detach(token)
        else:
            yield

    def __getattr__(self, fn_name: str) -> Callable[..., Any]:
        def call_with_context(*args: Any, **kwargs: Any) -> Any:
            self.logger.debug("Handling: %r", fn_name)

            handler_fn = getattr(self.handler, fn_name)

            span = self.context.span
            span.set_tag("thrift.method", fn_name)
            start_time = time.perf_counter()
            with self._set_remote_context(self.context) as ctx:
                with self.tracer.start_as_current_span(
                    fn_name, kind=trace.SpanKind.SERVER, record_exception=False
                ) as otelspan:
                    otelspan.set_attribute("thrift.method", fn_name)
                    if b"User-Agent" in self.context.headers:
                        otelspan.set_attribute(
                            "user.agent", self.context.headers[b"User-Agent"].decode()
                        )

                    try:
                        span.start()
                        with PROM_ACTIVE.labels(fn_name).track_inprogress():
                            result = handler_fn(self.context, *args, **kwargs)
                    except (TApplicationException, TProtocolException, TTransportException) as e:
                        # these are subclasses of TException but aren't ones that
                        # should be expected in the protocol
                        otelspan.record_exception(e)
                        span.finish(exc_info=sys.exc_info())
                        raise
                    except Error as exc:
                        c = ErrorCode()
                        status = c._VALUES_TO_NAMES.get(exc.code, "")

                        otelspan.set_attribute("exception_type", "Error")
                        otelspan.set_attribute("thrift.status_code", exc.code)
                        otelspan.set_attribute("thrift.status", status)

                        span.set_tag("exception_type", "Error")
                        span.set_tag("thrift.status_code", exc.code)
                        span.set_tag("thrift.status", status)
                        span.set_tag("success", "false")
                        # mark 5xx errors as failures since those are still "unexpected"
                        if 500 <= exc.code < 600:
                            otelspan.set_status(trace.status.Status(trace.status.StatusCode.ERROR))
                            otelspan.record_exception(exc)
                            span.finish(exc_info=sys.exc_info())
                        else:
                            # Set as OK as this is an expected exception
                            otelspan.set_status(trace.status.Status(trace.status.StatusCode.OK))
                            span.finish()
                        raise
                    except TException as exc:
                        otelspan.record_exception(exc)

                        span.set_tag("exception_type", type(exc).__name__)
                        span.set_tag("success", "false")

                        # this is an expected exception, as defined in the IDL
                        span.finish()
                        # Set as OK as this is an expected exception
                        otelspan.set_status(trace.status.Status(trace.status.StatusCode.OK))
                        raise
                    except Exception as e:  # noqa: E722
                        # the handler crashed (or timed out)!
                        span.finish(exc_info=sys.exc_info())

                        otelspan.record_exception(e)
                        otelspan.set_status(trace.status.Status(trace.status.StatusCode.ERROR))

                        if self.convert_to_baseplate_error:
                            raise Error(
                                code=ErrorCode.INTERNAL_SERVER_ERROR,
                                message="Internal server error",
                            )
                        raise
                    else:
                        # a normal result
                        otelspan.set_status(trace.status.Status(trace.status.StatusCode.OK))
                        span.finish()
                        return result
                    finally:
                        thrift_success = "true"
                        exception_type = ""
                        baseplate_status_code = ""
                        baseplate_status = ""
                        exc_info = sys.exc_info()
                        if exc_info[0] is not None:
                            thrift_success = "false"
                            exception_type = exc_info[0].__name__
                            current_exc = exc_info[1]
                            try:
                                # We want the following code to execute whenever the
                                # service raises an instance of Baseplate's `Error` class.
                                # Unfortunately, we cannot just rely on `isinstance` to do
                                # what we want here because some services compile
                                # Baseplate's thrift file on their own and import `Error`
                                # from that. When this is done, `isinstance` will always
                                # return `False` since it's technically a different class.
                                # To fix this, we optimistically try to access `code` on
                                # `current_exc` and just catch the `AttributeError` if the
                                # `code` attribute is not present.
                                # Note: if the error code was not originally defined in baseplate, or the
                                # name associated with the error was overriden, this cannot reflect that
                                # we will emit the status code in both cases
                                # but the status will be blank in the first case, and the baseplate name
                                # in the second
                                baseplate_status_code = current_exc.code  # type: ignore
                                baseplate_status = ErrorCode()._VALUES_TO_NAMES.get(current_exc.code, "")  # type: ignore
                            except AttributeError:
                                pass
                        PROM_REQUESTS.labels(
                            thrift_method=fn_name,
                            thrift_success=thrift_success,
                            thrift_exception_type=exception_type,
                            thrift_baseplate_status=baseplate_status,
                            thrift_baseplate_status_code=baseplate_status_code,
                        ).inc()
                        PROM_LATENCY.labels(fn_name, thrift_success).observe(
                            time.perf_counter() - start_time
                        )

        return call_with_context


def baseplateify_processor(
    processor: TProcessor,
    logger: Logger,
    baseplate: Baseplate,
    edge_context_factory: Optional[EdgeContextFactory] = None,
    convert_to_baseplate_error: bool = False,
) -> TProcessor:
    """Wrap a Thrift Processor with Baseplate's span lifecycle.

    :param processor: The service's processor to wrap.
    :param logger: The logger to use for error and debug logging.
    :param baseplate: The baseplate instance for your application.
    :param edge_context_factory: A configured factory for handling edge request
        context.
    :param convert_to_baseplate_error: If True, the server will automatically
        convert unhandled exceptions to:
            baseplate.Error(
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message="Internal server error",
            )

    """

    def make_processor_fn(fn_name: str, processor_fn: Callable[..., Any]) -> Callable[..., Any]:
        def call_processor_with_span_context(
            self: Any, seqid: int, iprot: TProtocolBase, oprot: TProtocolBase
        ) -> Any:
            context = baseplate.make_context_object()

            # Allow case-insensitivity for THeader headers
            headers: Mapping[bytes, bytes] = CaseInsensitiveDict(  # type: ignore
                data=iprot.get_headers()
            )

            trace_info: Optional[TraceInfo]
            try:
                sampled = bool(headers.get(b"Sampled") == b"1")
                flags = headers.get(b"Flags", None)
                trace_info = TraceInfo.from_upstream(
                    headers[b"Trace"].decode(),
                    headers[b"Parent"].decode(),
                    headers[b"Span"].decode(),
                    sampled,
                    int(flags) if flags is not None else None,
                )
            except (KeyError, ValueError):
                trace_info = None

            edge_payload = headers.get(b"Edge-Request", None)
            context.raw_edge_context = edge_payload
            if edge_context_factory:
                context.edge_context = edge_context_factory.from_upstream(edge_payload)

            try:
                raw_deadline_budget = headers[b"Deadline-Budget"].decode()
                context.deadline_budget = float(raw_deadline_budget) / 1000
            except (KeyError, ValueError):
                context.deadline_budget = None

            span = baseplate.make_server_span(context, name=fn_name, trace_info=trace_info)
            span.set_tag("protocol", "thrift")

            try:
                service_name = headers[b"User-Agent"].decode()
            except (KeyError, UnicodeDecodeError):
                pass
            else:
                span.set_tag("peer.service", service_name)

            context.headers = headers

            handler = processor._handler
            context_aware_handler = _ContextAwareHandler(
                handler, context, logger, convert_to_baseplate_error
            )
            context_aware_processor = processor.__class__(context_aware_handler)
            return processor_fn(context_aware_processor, seqid, iprot, oprot)

        return call_processor_with_span_context

    instrumented_process_map = {}
    for fn_name, processor_fn in processor._processMap.items():
        context_aware_processor_fn = make_processor_fn(fn_name, processor_fn)
        instrumented_process_map[fn_name] = context_aware_processor_fn
    processor._processMap = instrumented_process_map
    processor.baseplate = baseplate
    return processor
