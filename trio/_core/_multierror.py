import sys
import traceback
import textwrap
import warnings

import attr

# python traceback.TracebackException < 3.6.4 does not support unhashable exceptions
# see https://github.com/python/cpython/pull/4014 for details
if sys.version_info < (3, 6, 4):
    exc_key = lambda exc: exc
else:
    exc_key = id

################################################################
# MultiError
################################################################


def _filter_impl(handler, root_exc):
    # We have a tree of MultiError's, like:
    #
    #  MultiError([
    #      ValueError,
    #      MultiError([
    #          KeyError,
    #          ValueError,
    #      ]),
    #  ])
    #
    # or similar.
    #
    # We want to
    # 1) apply the filter to each of the leaf exceptions -- each leaf
    #    might stay the same, be replaced (with the original exception
    #    potentially sticking around as __context__ or __cause__), or
    #    disappear altogether.
    # 2) simplify the resulting tree -- remove empty nodes, and replace
    #    singleton MultiError's with their contents, e.g.:
    #        MultiError([KeyError]) -> KeyError
    #    (This can happen recursively, e.g. if the two ValueErrors above
    #    get caught then we'll just be left with a bare KeyError.)
    # 3) preserve sensible tracebacks
    #
    # It's the tracebacks that are most confusing. As a MultiError
    # propagates through the stack, it accumulates traceback frames, but
    # the exceptions inside it don't. Semantically, the traceback for a
    # leaf exception is the concatenation the tracebacks of all the
    # exceptions you see when traversing the exception tree from the root
    # to that leaf. Our correctness invariant is that this concatenated
    # traceback should be the same before and after.
    #
    # The easy way to do that would be to, at the beginning of this
    # function, "push" all tracebacks down to the leafs, so all the
    # MultiErrors have __traceback__=None, and all the leafs have complete
    # tracebacks. But whenever possible, we'd actually prefer to keep
    # tracebacks as high up in the tree as possible, because this lets us
    # keep only a single copy of the common parts of these exception's
    # tracebacks. This is cheaper (in memory + time -- tracebacks are
    # unpleasantly quadratic-ish to work with, and this might matter if
    # you have thousands of exceptions, which can happen e.g. after
    # cancelling a large task pool, and no-one will ever look at their
    # tracebacks!), and more importantly, factoring out redundant parts of
    # the tracebacks makes them more readable if/when users do see them.
    #
    # So instead our strategy is:
    # - first go through and construct the new tree, preserving any
    #   unchanged subtrees
    # - then go through the original tree (!) and push tracebacks down
    #   until either we hit a leaf, or we hit a subtree which was
    #   preserved in the new tree.

    # This used to also support async handler functions. But that runs into:
    #   https://bugs.python.org/issue29600
    # which is difficult to fix on our end.

    # Filters a subtree, ignoring tracebacks, while keeping a record of
    # which MultiErrors were preserved unchanged
    def filter_tree(exc, preserved):
        if isinstance(exc, MultiError):
            new_exceptions = []
            changed = False
            for child_exc in exc.exceptions:
                new_child_exc = filter_tree(child_exc, preserved)
                if new_child_exc is not child_exc:
                    changed = True
                if new_child_exc is not None:
                    new_exceptions.append(new_child_exc)
            if not new_exceptions:
                return None
            elif changed:
                return MultiError(new_exceptions)
            else:
                preserved.add(id(exc))
                return exc
        else:
            new_exc = handler(exc)
            # Our version of implicit exception chaining
            if new_exc is not None and new_exc is not exc:
                new_exc.__context__ = exc
            return new_exc

    def push_tb_down(tb, exc, preserved):
        if id(exc) in preserved:
            return
        new_tb = concat_tb(tb, exc.__traceback__)
        if isinstance(exc, MultiError):
            for child_exc in exc.exceptions:
                push_tb_down(new_tb, child_exc, preserved)
            exc.__traceback__ = None
        else:
            exc.__traceback__ = new_tb

    preserved = set()
    new_root_exc = filter_tree(root_exc, preserved)
    push_tb_down(None, root_exc, preserved)
    # Delete the local functions to avoid a reference cycle (see
    # test_simple_cancel_scope_usage_doesnt_create_cyclic_garbage)
    del filter_tree, push_tb_down
    return new_root_exc


# Normally I'm a big fan of (a)contextmanager, but in this case I found it
# easier to use the raw context manager protocol, because it makes it a lot
# easier to reason about how we're mutating the traceback as we go. (End
# result: if the exception gets modified, then the 'raise' here makes this
# frame show up in the traceback; otherwise, we leave no trace.)
@attr.s(frozen=True)
class MultiErrorCatcher:
    _handler = attr.ib()

    def __enter__(self):
        pass

    def __exit__(self, etype, exc, tb):
        if exc is not None:
            filtered_exc = MultiError.filter(self._handler, exc)

            if filtered_exc is exc:
                # Let the interpreter re-raise it
                return False
            if filtered_exc is None:
                # Swallow the exception
                return True
            # When we raise filtered_exc, Python will unconditionally blow
            # away its __context__ attribute and replace it with the original
            # exc we caught. So after we raise it, we have to pause it while
            # it's in flight to put the correct __context__ back.
            old_context = filtered_exc.__context__
            try:
                raise filtered_exc
            finally:
                _, value, _ = sys.exc_info()
                assert value is filtered_exc
                value.__context__ = old_context
                # delete references from locals to avoid creating cycles
                # see test_MultiError_catch_doesnt_create_cyclic_garbage
                del _, filtered_exc, value


class MultiError(BaseException):
    """An exception that contains other exceptions; also known as an
    "inception".

    It's main use is to represent the situation when multiple child tasks all
    raise errors "in parallel".

    Args:
      exceptions (list): The exceptions

    Returns:
      If ``len(exceptions) == 1``, returns that exception. This means that a
      call to ``MultiError(...)`` is not guaranteed to return a
      :exc:`MultiError` object!

      Otherwise, returns a new :exc:`MultiError` object.

    Raises:
      TypeError: if any of the passed in objects are not instances of
          :exc:`BaseException`.

    """

    def __init__(self, exceptions):
        # Avoid recursion when exceptions[0] returned by __new__() happens
        # to be a MultiError and subsequently __init__() is called.
        if hasattr(self, "exceptions"):
            # __init__ was already called on this object
            assert len(exceptions) == 1 and exceptions[0] is self
            return
        self.exceptions = exceptions

    def __new__(cls, exceptions):
        exceptions = list(exceptions)
        for exc in exceptions:
            if not isinstance(exc, BaseException):
                raise TypeError("Expected an exception object, not {!r}".format(exc))
        if len(exceptions) == 1:
            # If this lone object happens to itself be a MultiError, then
            # Python will implicitly call our __init__ on it again.  See
            # special handling in __init__.
            return exceptions[0]
        else:
            # The base class __new__() implicitly invokes our __init__, which
            # is what we want.
            #
            # In an earlier version of the code, we didn't define __init__ and
            # simply set the `exceptions` attribute directly on the new object.
            # However, linters expect attributes to be initialized in __init__.
            return BaseException.__new__(cls, exceptions)

    def __str__(self):
        return ", ".join(repr(exc) for exc in self.exceptions)

    def __repr__(self):
        return "<MultiError: {}>".format(self)

    @classmethod
    def filter(cls, handler, root_exc):
        """Apply the given ``handler`` to all the exceptions in ``root_exc``.

        Args:
          handler: A callable that takes an atomic (non-MultiError) exception
              as input, and returns either a new exception object or None.
          root_exc: An exception, often (though not necessarily) a
              :exc:`MultiError`.

        Returns:
          A new exception object in which each component exception ``exc`` has
          been replaced by the result of running ``handler(exc)`` – or, if
          ``handler`` returned None for all the inputs, returns None.

        """

        return _filter_impl(handler, root_exc)

    @classmethod
    def catch(cls, handler):
        """Return a context manager that catches and re-throws exceptions
        after running :meth:`filter` on them.

        Args:
          handler: as for :meth:`filter`

        """

        return MultiErrorCatcher(handler)


# Clean up exception printing:
MultiError.__module__ = "trio"

################################################################
# concat_tb
################################################################

# We need to compute a new traceback that is the concatenation of two existing
# tracebacks. This requires copying the entries in 'head' and then pointing
# the final tb_next to 'tail'.
#
# NB: 'tail' might be None, which requires some special handling in the ctypes
# version.
#
# The complication here is that Python doesn't actually support copying or
# modifying traceback objects, so we have to get creative...
#
# On CPython, we use ctypes. On PyPy, we use "transparent proxies".
#
# Jinja2 is a useful source of inspiration:
#   https://github.com/pallets/jinja/blob/master/jinja2/debug.py

try:
    import tputil
except ImportError:
    have_tproxy = False
else:
    have_tproxy = True

if have_tproxy:
    # http://doc.pypy.org/en/latest/objspace-proxies.html
    def copy_tb(base_tb, tb_next):
        def controller(operation):
            # Rationale for pragma: I looked fairly carefully and tried a few
            # things, and AFAICT it's not actually possible to get any
            # 'opname' that isn't __getattr__ or __getattribute__. So there's
            # no missing test we could add, and no value in coverage nagging
            # us about adding one.
            if operation.opname in [
                "__getattribute__",
                "__getattr__",
            ]:  # pragma: no cover
                if operation.args[0] == "tb_next":
                    return tb_next
            return operation.delegate()

        return tputil.make_proxy(controller, type(base_tb), base_tb)


else:
    # ctypes it is
    import ctypes

    # How to handle refcounting? I don't want to use ctypes.py_object because
    # I don't understand or trust it, and I don't want to use
    # ctypes.pythonapi.Py_{Inc,Dec}Ref because we might clash with user code
    # that also tries to use them but with different types. So private _ctypes
    # APIs it is!
    import _ctypes

    class CTraceback(ctypes.Structure):
        _fields_ = [
            ("PyObject_HEAD", ctypes.c_byte * object().__sizeof__()),
            ("tb_next", ctypes.c_void_p),
            ("tb_frame", ctypes.c_void_p),
            ("tb_lasti", ctypes.c_int),
            ("tb_lineno", ctypes.c_int),
        ]

    def copy_tb(base_tb, tb_next):
        # TracebackType has no public constructor, so allocate one the hard way
        try:
            raise ValueError
        except ValueError as exc:
            new_tb = exc.__traceback__
        c_new_tb = CTraceback.from_address(id(new_tb))

        # At the C level, tb_next either pointer to the next traceback or is
        # NULL. c_void_p and the .tb_next accessor both convert NULL to None,
        # but we shouldn't DECREF None just because we assigned to a NULL
        # pointer! Here we know that our new traceback has only 1 frame in it,
        # so we can assume the tb_next field is NULL.
        assert c_new_tb.tb_next is None
        # If tb_next is None, then we want to set c_new_tb.tb_next to NULL,
        # which it already is, so we're done. Otherwise, we have to actually
        # do some work:
        if tb_next is not None:
            _ctypes.Py_INCREF(tb_next)
            c_new_tb.tb_next = id(tb_next)

        assert c_new_tb.tb_frame is not None
        _ctypes.Py_INCREF(base_tb.tb_frame)
        old_tb_frame = new_tb.tb_frame
        c_new_tb.tb_frame = id(base_tb.tb_frame)
        _ctypes.Py_DECREF(old_tb_frame)

        c_new_tb.tb_lasti = base_tb.tb_lasti
        c_new_tb.tb_lineno = base_tb.tb_lineno

        try:
            return new_tb
        finally:
            # delete references from locals to avoid creating cycles
            # see test_MultiError_catch_doesnt_create_cyclic_garbage
            del new_tb, old_tb_frame


def concat_tb(head, tail):
    # We have to use an iterative algorithm here, because in the worst case
    # this might be a RecursionError stack that is by definition too deep to
    # process by recursion!
    head_tbs = []
    pointer = head
    while pointer is not None:
        head_tbs.append(pointer)
        pointer = pointer.tb_next
    current_head = tail
    for head_tb in reversed(head_tbs):
        current_head = copy_tb(head_tb, tb_next=current_head)
    return current_head


################################################################
# MultiError traceback formatting
#
# What follows is terrible, terrible monkey patching of
# traceback.TracebackException to add support for handling
# MultiErrors
################################################################

traceback_exception_original_init = traceback.TracebackException.__init__


def traceback_exception_init(
    self,
    exc_type,
    exc_value,
    exc_traceback,
    *,
    limit=None,
    lookup_lines=True,
    capture_locals=False,
    compact=False,
    _seen=None,
):
    if sys.version_info >= (3, 10):
        kwargs = {"compact": compact}
    else:
        kwargs = {}

    # Capture the original exception and its cause and context as TracebackExceptions
    traceback_exception_original_init(
        self,
        exc_type,
        exc_value,
        exc_traceback,
        limit=limit,
        lookup_lines=lookup_lines,
        capture_locals=capture_locals,
        _seen=_seen,
        **kwargs,
    )

    seen_was_none = _seen is None

    if _seen is None:
        _seen = set()

    # Capture each of the exceptions in the MultiError along with each of their causes and contexts
    if isinstance(exc_value, MultiError):
        embedded = []
        for exc in exc_value.exceptions:
            if exc_key(exc) not in _seen:
                embedded.append(
                    traceback.TracebackException.from_exception(
                        exc,
                        limit=limit,
                        lookup_lines=lookup_lines,
                        capture_locals=capture_locals,
                        # copy the set of _seen exceptions so that duplicates
                        # shared between sub-exceptions are not omitted
                        _seen=None if seen_was_none else set(_seen),
                    )
                )
        self.embedded = embedded
    else:
        self.embedded = []


traceback.TracebackException.__init__ = traceback_exception_init  # type: ignore
traceback_exception_original_format = traceback.TracebackException.format


def traceback_exception_format(self, *, chain=True):
    yield from traceback_exception_original_format(self, chain=chain)

    for i, exc in enumerate(self.embedded):
        yield "\nDetails of embedded exception {}:\n\n".format(i + 1)
        yield from (textwrap.indent(line, " " * 2) for line in exc.format(chain=chain))


traceback.TracebackException.format = traceback_exception_format  # type: ignore


def trio_excepthook(etype, value, tb):
    for chunk in traceback.format_exception(etype, value, tb):
        sys.stderr.write(chunk)


monkeypatched_or_warned = False

if "IPython" in sys.modules:
    import IPython

    ip = IPython.get_ipython()
    if ip is not None:
        if ip.custom_exceptions != ():
            warnings.warn(
                "IPython detected, but you already have a custom exception "
                "handler installed. I'll skip installing Trio's custom "
                "handler, but this means MultiErrors will not show full "
                "tracebacks.",
                category=RuntimeWarning,
            )
            monkeypatched_or_warned = True
        else:

            def trio_show_traceback(self, etype, value, tb, tb_offset=None):
                # XX it would be better to integrate with IPython's fancy
                # exception formatting stuff (and not ignore tb_offset)
                trio_excepthook(etype, value, tb)

            ip.set_custom_exc((MultiError,), trio_show_traceback)
            monkeypatched_or_warned = True

if sys.excepthook is sys.__excepthook__:
    sys.excepthook = trio_excepthook
    monkeypatched_or_warned = True

# Ubuntu's system Python has a sitecustomize.py file that import
# apport_python_hook and replaces sys.excepthook.
#
# The custom hook captures the error for crash reporting, and then calls
# sys.__excepthook__ to actually print the error.
#
# We don't mind it capturing the error for crash reporting, but we want to
# take over printing the error. So we monkeypatch the apport_python_hook
# module so that instead of calling sys.__excepthook__, it calls our custom
# hook.
#
# More details: https://github.com/python-trio/trio/issues/1065
if getattr(sys.excepthook, "__name__", None) == "apport_excepthook":
    import apport_python_hook

    assert sys.excepthook is apport_python_hook.apport_excepthook

    # Give it a descriptive name as a hint for anyone who's stuck trying to
    # debug this mess later.
    class TrioFakeSysModuleForApport:
        pass

    fake_sys = TrioFakeSysModuleForApport()
    fake_sys.__dict__.update(sys.__dict__)
    fake_sys.__excepthook__ = trio_excepthook  # type: ignore
    apport_python_hook.sys = fake_sys

    monkeypatched_or_warned = True

if not monkeypatched_or_warned:
    warnings.warn(
        "You seem to already have a custom sys.excepthook handler "
        "installed. I'll skip installing Trio's custom handler, but this "
        "means MultiErrors will not show full tracebacks.",
        category=RuntimeWarning,
    )
