import collections
import enum
import inspect
import types


class FutureState(enum.Enum):
    # Todo CANCELLED
    PENDING = 'pending'
    FINISHED = 'finished'


class Future:
    """
    A future represents the result of some computation that may become available in *drumroll* the future.

    A future starts out in PENDING state.

    Calling set_result(v) transitions it to FINISHED state and
    will resume dependent coroutines (i.e. those waiting on this Future)
    at the next Scheduler.tick().

    Calling set_exception(e) transitions it to FINISHED state as well
    and generally works the same way as set_result(), but instead
    of providing a value to dependent coroutines, they will blow
    up with an exception.

    result() retrieves the current result *or* raises the set exception;
    ``x = await future()`` is semantically equivalent to
    ``twiddle_thumbs_until(future.is.done); x = future.result()``.
    """
    _state: FutureState = FutureState.PENDING
    _result = None
    _exception = None
    _scheduler = None

    def __init__(self, scheduler=None):
        self._scheduler = scheduler
        self._done_callbacks = []

    def done(self):
        """
        A future is done if it (1) finished (with a result or exception) (2) was cancelled.
        """
        return self._state != FutureState.PENDING

    def result(self):
        assert self._state == FutureState.FINISHED
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self):
        assert self._state == FutureState.FINISHED
        return self._exception

    def add_done_callback(self, fn):
        """
        Add callback *fn* that will be run when the future enters a done state.

        The callback receives the futures as its sole argument.
        """
        if self._state != FutureState.PENDING:
            fn(self)
        else:
            self._done_callbacks.append(fn)

    def set_result(self, result):
        assert self._state == FutureState.PENDING
        self._state = FutureState.FINISHED
        self._result = result
        self._schedule_callbacks()

    def set_exception(self, exception):
        if isinstance(exception, type):
            exception = exception()
        self._state = FutureState.FINISHED
        self._exception = exception
        self._schedule_callbacks()

    def _schedule_callbacks(self):
        for cb in self._done_callbacks:
            self._scheduler.call_soon(cb, self)

    def _tick(self):
        pass

    def __await__(self):
        if not self.done():
            yield self
        return self.result()


class Task(Future):
    """
    A Task is a concretisation  of the Future concept;
    it represents a thread (no, wait, loaded word)... uhm...
    a *line* (fiber? oops...) of execution.

    In other words, a Task object corresponds to a coroutine *invocation*
    (the terminology is pretty loose here, in Python's terms, "async_foo()" *is* a coroutine),
    so it is a concrete computation that can be advanced in response to Futures finishing.

    The result() of a Task is the ``return`` value of the coroutine it represents.
    """

    def __init__(self, coroutine, scheduler):
        super().__init__(scheduler)
        self._coroutine = coroutine

    def _tick(self, source_future=None):
        if source_future:
            exc = source_future.exception()
            value = source_future._result
        else:
            exc = value = None

        try:
            if exc is not None:
                self._coroutine.throw(exc)
            else:
                result = self._coroutine.send(value)
        except StopIteration as si:
            self.set_result(si.value)
        except Exception as exc:
            self.set_exception(exc)
        else:
            if isinstance(result, Future):
                result._scheduler = self._scheduler
                result.add_done_callback(lambda src: self._scheduler._queue_task(self, src))
            elif inspect.iscoroutine(result):
                f = ensure_future(result, self)
                f.add_done_callback(lambda src: self._scheduler._queue_task(self, src))
            else:
                self._scheduler._queue_task(self)


def ensure_future(future_or_coroutine, scheduler):
    """
    Convert *future_or_coroutine* to a Future instance belonging to *scheduler*.

    If *future_or_coroutine* is not a coroutine, it is assumed to be a Future
    and consequently adopted to *scheduler*.
    """
    if inspect.iscoroutine(future_or_coroutine):
        return Task(future_or_coroutine, scheduler)
    future_or_coroutine._scheduler = scheduler
    return future_or_coroutine


@types.coroutine
def suspend():
    """
    Yield control to the scheduler for one iteration.
    """
    yield


class Scheduler:
    def __init__(self):
        self._queue = collections.deque()
        self._blocked = []

    def tick(self):
        """
        Perform all currently pending actions, e.g. resuming runnable coroutines.
        """
        while self._queue:
            cb, args = self._queue.popleft()
            cb(*args)

    def run_until_complete(self, future_or_coroutine):
        """
        Run *future_or_coroutine* until completion.
        """
        future = ensure_future(future_or_coroutine, self)
        self._queue_task(future)
        while not future.done():
            self.tick()
        return future.result()

    def run(self, future_or_coroutine):
        """
        Add *future_or_coroutine* to the running set of futures.
        """
        future = ensure_future(future_or_coroutine, self)
        self._queue_task(future)
        return future

    def call_soon(self, cb, *args):
        """
        Call *cb* with *args* next time a .tick() happens.
        """
        self._queue.append((cb, args))

    def _queue_task(self, task_future, src=None):
        self._queue.append((task_future._tick, (src,)))
