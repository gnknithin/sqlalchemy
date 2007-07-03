"""Support for attributes that hold collections of objects."""

from sqlalchemy import exceptions, schema, util as sautil
from sqlalchemy.orm import mapper
import copy, sys, warnings, weakref
import new

try:
    from threading import Lock
except:
    from dummy_threading import Lock
try:
    from operator import attrgetter
except:
    def attrgetter(attribute):
        return lambda value: getattr(value, attribute)


__all__ = ['collection', 'collection_adapter',
           'mapped_collection', 'column_mapped_collection',
           'attribute_mapped_collection']
           
def column_mapped_collection(mapping_spec):
    """A dictionary-based collection type with column-based keying.

    Returns a MappedCollection factory with a keying function generated
    from mapping_spec, which may be a Column or a sequence of Columns.

    The key value must be immutable for the lifetime of the object.  You
    can not, for example, map on foreign key values if those key values will
    change during the session, i.e. from None to a database-assigned integer
    after a session flush.
    """

    if isinstance(mapping_spec, schema.Column):
        def keyfunc(value):
            m = mapper.object_mapper(value)
            return m.get_attr_by_column(value, mapping_spec)
    else:
        cols = []
        for c in mapping_spec:
            if not isinstance(c, schema.Column):
                raise exceptions.ArgumentError(
                    "mapping_spec tuple may only contain columns")
            cols.append(c)
        mapping_spec = tuple(cols)
        def keyfunc(value):
            m = mapper.object_mapper(value)
            return tuple([m.get_attr_by_column(value, c) for c in mapping_spec])
    return lambda: MappedCollection(keyfunc)

def attribute_mapped_collection(attr_name):
    """A dictionary-based collection type with attribute-based keying.

    Returns a MappedCollection factory with a keying based on the
    'attr_name' atribute of entities in the collection.

    The key value must be immutable for the lifetime of the object.  You
    can not, for example, map on foreign key values if those key values will
    change during the session, i.e. from None to a database-assigned integer
    after a session flush.
    """

    return lambda: MappedCollection(attrgetter(attr_name))


def mapped_collection(keyfunc):
    """A dictionary-based collection type with arbitrary keying.

    Returns a MappedCollection factory with a keying function generated
    from keyfunc, a callable that takes an entity and returns a key value.

    The key value must be immutable for the lifetime of the object.  You
    can not, for example, map on foreign key values if those key values will
    change during the session, i.e. from None to a database-assigned integer
    after a session flush.
    """

    return lambda: MappedCollection(keyfunc)

class collection(object):
    """Decorators for custom collection classes.

    The decorators fall into two groups: annotations and interception recipes.

    The annotating decorators (appender, remover, iterator,
    internally_instrumented) indicate the method's purpose and take no
    arguments.  They are not written with parens:

        @collection.appender
        def append(self, append): ...

    The recipe decorators all require parens, even those that take no
    arguments:

        @collection.adds('entity'):
        def insert(self, position, entity): ...

        @collection.removes_return()
        def popitem(self): ...

    Decorators can be specified in long-hand for Python 2.3, or with
    the class-level dict attribute '__instrumentation__'- see the source
    for details.
    """

    # Bundled as a class solely for ease of use: packaging, doc strings,
    # importability.
    
    def appender(cls, fn):
        """Tag the method as the collection appender.

        The appender method is called with one positional argument: the value
        to append. The method will be automatically decorated with 'adds(1)'
        if not already decorated.

            @collection.appender
            def add(self, append): ...

            # or, equivalently
            @collection.appender
            @collection.adds(1)
            def add(self, append): ...

            # for mapping type, an 'append' may kick out a previous value
            # that occupies that slot.  consider d['a'] = 'foo'- any previous
            # value in d['a'] is discarded.
            @collection.appender
            @collection.replaces(1)
            def add(self, entity):
                key = some_key_func(entity)
                previous = None
                if key in self:
                    previous = self[key]
                self[key] = entity
                return previous

        If the value to append is not allowed in the collection, you may
        raise an exception.  Something to remember is that the appender
        will be called for each object mapped by a database query.  If the
        database contains rows that violate your collection semantics, you
        will need to get creative to fix the problem, as access via the
        collection will not work.
     
        If the appender method is internally instrumented, you must also
        receive the keyword argument '_sa_initiator' and ensure its
        promulgation to collection events.
        """

        setattr(fn, '_sa_instrument_role', 'appender')
        return fn
    appender = classmethod(appender)

    def remover(cls, fn):
        """Tag the method as the collection remover.

        The remover method is called with one positional argument: the value
        to remove. The method will be automatically decorated with
        'removes_return()' if not already decorated.

            @collection.remover
            def zap(self, entity): ...

            # or, equivalently
            @collection.remover
            @collection.removes_return()
            def zap(self, ): ...

        If the value to remove is not present in the collection, you may
        raise an exception or return None to ignore the error.

        If the remove method is internally instrumented, you must also
        receive the keyword argument '_sa_initiator' and ensure its
        promulgation to collection events.
        """
        
        setattr(fn, '_sa_instrument_role', 'remover')
        return fn
    remover = classmethod(remover)

    def iterator(cls, fn):
        """Tag the method as the collection remover.

        The iterator method is called with no arguments.  It is expected to
        return an iterator over all collection members.

            @collection.iterator
            def __iter__(self): ...
        """

        setattr(fn, '_sa_instrument_role', 'iterator')
        return fn
    iterator = classmethod(iterator)

    def internally_instrumented(cls, fn):
        """Tag the method as instrumented.

        This tag will prevent any decoration from being applied to the method.
        Use this if you are orchestrating your own calls to collection_adapter
        in one of the basic SQLAlchemy interface methods, or to prevent
        an automatic ABC method decoration from wrapping your implementation.

            # normally an 'extend' method on a list-like class would be
            # automatically intercepted and re-implemented in terms of
            # SQLAlchemy events and append().  your implementation will
            # never be called, unless:
            @collection.internally_instrumented
            def extend(self, items): ...
        """
        
        setattr(fn, '_sa_instrumented', True)
        return fn
    internally_instrumented = classmethod(internally_instrumented)

    def on_link(cls, fn):
        """Tag the method as a the "linked to attribute" event handler.

        This optional event handler will be called when the collection class
        is linked to or unlinked from the InstrumentedAttribute.  It is
        invoked immediately after the '_sa_adapter' property is set on
        the instance.  A single argument is passed: the collection adapter
        that has been linked, or None if unlinking.
        """
        
        setattr(fn, '_sa_instrument_role', 'on_link')
        return fn
    on_link = classmethod(on_link)

    def adds(cls, arg):
        """Mark the method as adding an entity to the collection.

        Adds "add to collection" handling to the method.  The decorator argument
        indicates which method argument holds the SQLAlchemy-relevant value.
        Arguments can be specified positionally (i.e. integer) or by name.

            @collection.adds(1)
            def push(self, item): ...

            @collection.adds('entity')
            def do_stuff(self, thing, entity=None): ...
        """

        def decorator(fn):
            setattr(fn, '_sa_instrument_before', ('fire_append_event', arg))
            return fn
        return decorator
    adds = classmethod(adds)

    def replaces(cls, arg):
        """Mark the method as replacing an entity in the collection.

        Adds "add to collection" and "remove from collection" handling to
        the method.  The decorator argument indicates which method argument
        holds the SQLAlchemy-relevant value to be added, and return value, if
        any will be considered the value to remove.
        
        Arguments can be specified positionally (i.e. integer) or by name.

            @collection.replaces(2)
            def __setitem__(self, index, item): ...
        """
        
        def decorator(fn):
            setattr(fn, '_sa_instrument_before', ('fire_append_event', arg))
            setattr(fn, '_sa_instrument_after', 'fire_remove_event')
            return fn
        return decorator
    replaces = classmethod(replaces)

    def removes(cls, arg):
        """Mark the method as removing an entity in the collection.

        Adds "remove from collection" handling to the method.  The decorator
        argument indicates which method argument holds the SQLAlchemy-relevant
        value to be removed. Arguments can be specified positionally (i.e.
        integer) or by name.

            @collection.removes(1)
            def zap(self, item): ...

        For methods where the value to remove is not known at call-time, use
        collection.removes_return.
        """

        def decorator(fn):
            setattr(fn, '_sa_instrument_before', ('fire_remove_event', arg))
            return fn
        return decorator
    removes = classmethod(removes)
    
    def removes_return(cls):
        """Mark the method as removing an entity in the collection.

        Adds "remove from collection" handling to the method.  The return value
        of the method, if any, is considered the value to remove.  The method
        arguments are not inspected.

            @collection.removes_return()
            def pop(self): ...

        For methods where the value to remove is known at call-time, use
        collection.remove.
        """

        def decorator(fn):
            setattr(fn, '_sa_instrument_after', 'fire_remove_event')
            return fn
        return decorator
    removes_return = classmethod(removes_return)


# public instrumentation interface for 'internally instrumented'
# implementations
def collection_adapter(collection):
    """Fetch the CollectionAdapter for a collection."""

    return getattr(collection, '_sa_adapter', None)

class CollectionAdaptor(object):
    """Bridges between the orm and arbitrary Python collections.

    Proxies base-level collection operations (append, remove, iterate)
    to the underlying Python collection, and emits add/remove events for
    entities entering or leaving the collection.
    """

    def __init__(self, attr, owner, data):
        self.attr = attr
        self._owner = weakref.ref(owner)
        self._data = weakref.ref(data)
        self.link_to_self(data)

    owner = property(lambda s: s._owner())
    data = property(lambda s: s._data())

    def link_to_self(self, data):
        setattr(data, '_sa_adapter', self)
        if hasattr(data, '_sa_on_link'):
            getattr(data, '_sa_on_link')(self)

    def unlink(self, data):
        setattr(data, '_sa_adapter', None)
        if hasattr(data, '_sa_on_link'):
            getattr(data, '_sa_on_link')(None)

    def append_with_event(self, item, initiator=None):
        getattr(self._data(), '_sa_appender')(item, _sa_initiator=initiator)

    def append_without_event(self, item):
        getattr(self._data(), '_sa_appender')(item, _sa_initiator=False)

    def remove_with_event(self, item, initiator=None):
        getattr(self._data(), '_sa_remover')(item, _sa_initiator=initiator)

    def remove_without_event(self, item):
        getattr(self._data(), '_sa_remover')(item, _sa_initiator=False)

    def clear_with_event(self, initiator=None):
        for item in list(self):
            self.remove_with_event(item, initiator)

    def clear_without_event(self):
        for item in list(self):
            self.remove_without_event(item)

    def __iter__(self):
        return getattr(self._data(), '_sa_iterator')()

    def __len__(self):
        return len(list(getattr(self._data(), '_sa_iterator')()))

    def __nonzero__(self):
        return True

    def fire_append_event(self, item, initiator=None):
        if initiator is not False and item is not None:
            self.attr.fire_append_event(self._owner(), item, initiator)

    def fire_remove_event(self, item, initiator=None):
        if initiator is not False and item is not None:
            self.attr.fire_remove_event(self._owner(), item, initiator)
    
    def __getstate__(self):
        return { 'key': self.attr.key,
                 'owner': self.owner,
                 'data': self.data }

    def __setstate__(self, d):
        self.attr = getattr(d['owner'].__class__, d['key'])
        self._owner = weakref.ref(d['owner'])
        self._data = weakref.ref(d['data'])


__instrumentation_mutex = Lock()
def _prepare_instrumentation(factory):
    """Prepare a callable for future use as a collection class factory.

    Given a collection class factory (either a type or no-arg callable),
    return another factory that will produce compatible instances when
    called.

    This function is responsible for converting collection_class=list
    into the run-time behavior of collection_class=InstrumentedList.
    """

    # Convert a builtin to 'Instrumented*'
    if factory in __canned_instrumentation:
        factory = __canned_instrumentation[factory]

    # Create a specimen
    cls = type(factory())

    # Did factory callable return a builtin?
    if cls in __canned_instrumentation:
        # Wrap it so that it returns our 'Instrumented*'
        factory = __converting_factory(factory)
        cls = factory()

    # Instrument the class if needed.
    if __instrumentation_mutex.acquire():
        try:
            if getattr(cls, '_sa_instrumented', None) != id(cls):
                _instrument_class(cls)
        finally:
            __instrumentation_mutex.release()

    return factory

def __converting_factory(original_factory):
    """Convert the type returned by collection factories on the fly.

    Given a collection factory that returns a builtin type (e.g. a list),
    return a wrapped function that converts that type to one of our
    instrumented types.
    """

    def wrapper():
        collection = original_factory()
        type_ = type(collection)
        if type_ in __canned_instrumentation:
            # return an instrumented type initialized from the factory's
            # collection
            return __canned_instrumentation[type_](collection)
        else:
            raise exceptions.InvalidRequestError(
                "Collection class factories must produce instances of a "
                "single class.")
    try:
        # often flawed but better than nothing
        wrapper.__name__ = "%sWrapper" % original_factory.__name__
        wrapper.__doc__ = original_factory.__doc__
    except:
        pass
    return wrapper

def _instrument_class(cls):
    # FIXME: more formally document this as a decoratorless/Python 2.3
    # option for specifying instrumentation.  (likely doc'd here in code only,
    # not in online docs.)
    # 
    # __instrumentation__ = {
    #   'rolename': 'methodname', # ...
    #   'methods': {
    #     'methodname': ('fire_{append,remove}_event', argspec,
    #                    'fire_{append,remove}_event'),
    #     'append': ('fire_append_event', 1, None),
    #     '__setitem__': ('fire_append_event', 1, 'fire_remove_event'),
    #     'pop': (None, None, 'fire_remove_event'),
    #     }
    #  }

    # In the normal call flow, a request for any of the 3 basic collection
    # types is transformed into one of our trivial subclasses
    # (e.g. InstrumentedList).  Catch anything else that sneaks in here...
    if cls.__module__ == '__builtin__':
        raise exceptions.ArgumentError(
            "Can not instrument a built-in type. Use a "
            "subclass, even a trivial one.")
    
    collection_type = sautil.duck_type_collection(cls)
    if collection_type in __interfaces:
        roles = __interfaces[collection_type].copy()
        decorators = roles.pop('_decorators', {})
    else:
        roles, decorators = {}, {}

    if hasattr(cls, '__instrumentation__'):
        roles.update(copy.deepcopy(getattr(cls, '__instrumentation__')))

    methods = roles.pop('methods', {})

    for name in dir(cls):
        method = getattr(cls, name)
        if not callable(method):
            continue

        # note role declarations
        if hasattr(method, '_sa_instrument_role'):
            role = method._sa_instrument_role
            assert role in ('appender', 'remover', 'iterator', 'on_link')
            roles[role] = name

        # transfer instrumentation requests from decorated function
        # to the combined queue
        before, after = None, None
        if hasattr(method, '_sa_instrument_before'):
            op, argument = method._sa_instrument_before
            assert op in ('fire_append_event', 'fire_remove_event')
            before = op, argument
        if hasattr(method, '_sa_instrument_after'):
            op = method._sa_instrument_after
            assert op in ('fire_append_event', 'fire_remove_event')
            after = op
        if before or after:
            methods[name] = before[0], before[1], after

    # apply ABC auto-decoration to methods that need it
    for method, decorator in decorators.items():
        fn = getattr(cls, method, None)
        if fn and method not in methods and not hasattr(fn, '_sa_instrumented'):
            setattr(cls, method, decorator(fn))

    # ensure all roles are present, and apply implicit instrumentation if
    # needed
    if 'appender' not in roles or not hasattr(cls, roles['appender']):
        raise exceptions.ArgumentError(
            "Type %s must elect an appender method to be "
            "a collection class" % cls.__name__)
    elif (roles['appender'] not in methods and
          not hasattr(getattr(cls, roles['appender']), '_sa_instrumented')):
        methods[roles['appender']] = ('fire_append_event', 1, None)

    if 'remover' not in roles or not hasattr(cls, roles['remover']):
        raise exceptions.ArgumentError(
            "Type %s must elect a remover method to be "
            "a collection class" % cls.__name__)
    elif (roles['remover'] not in methods and
          not hasattr(getattr(cls, roles['remover']), '_sa_instrumented')):
        methods[roles['remover']] = ('fire_remove_event', 1, None)

    if 'iterator' not in roles or not hasattr(cls, roles['iterator']):
        raise exceptions.ArgumentError(
            "Type %s must elect an iterator method to be "
            "a collection class" % cls.__name__)

    # apply ad-hoc instrumentation from decorators, class-level defaults
    # and implicit role declarations
    for method, (before, argument, after) in methods.items():
        setattr(cls, method,
                _instrument_membership_mutator(getattr(cls, method),
                                               before, argument, after))    
    # intern the role map
    for role, method in roles.items():
        setattr(cls, '_sa_%s' % role, getattr(cls, method))

    setattr(cls, '_sa_instrumented', id(cls))

def _instrument_membership_mutator(method, before, argument, after):
    """Route method args and/or return value through the collection adapter."""

    if type(argument) is int:
        def wrapper(*args, **kw):
            if before and len(args) < argument:
                raise exceptions.ArgumentError(
                    'Missing argument %i' % argument)
            initiator = kw.pop('_sa_initiator', None)
            if initiator is False:
                executor = None
            else:
                executor = getattr(args[0], '_sa_adapter', None)
            
            if before and executor:
                getattr(executor, before)(args[argument], initiator)

            if not after or not executor:
                return method(*args, **kw)
            else:
                res = method(*args, **kw)
                if res is not None:
                    getattr(executor, after)(res, initiator)
                return res
    else:
        def wrapper(*args, **kw):
            if before:
                vals = inspect.getargvalues(inspect.currentframe())
                if argument in kw:
                    value = kw[argument]
                else:
                    positional = inspect.getargspec(method)[0]
                    pos = positional.index(argument)
                    if pos == -1:
                        raise exceptions.ArgumentError('Missing argument %s' %
                                                       argument)
                    else:
                        value = args[pos]

            initiator = kw.pop('_sa_initiator', None)
            if initiator is False:
                executor = None
            else:
                executor = getattr(args[0], '_sa_adapter', None)

            if before and executor:
                getattr(executor, op)(value, initiator)

            if not after or not executor:
                return method(*args, **kw)
            else:
                res = method(*args, **kw)
                if res is not None:
                    getattr(executor, after)(res, initiator)
                return res
    try:
        wrapper._sa_instrumented = True
        wrapper.__name__ = method.__name__
        wrapper.__doc__ = method.__doc__
    except:
        pass
    return wrapper

def __set(collection, item, _sa_initiator=None):
    """Run set events, may eventually be inlined into decorators."""

    if _sa_initiator is not False and item is not None:
        executor = getattr(collection, '_sa_adapter', None)
        if executor:
            getattr(executor, 'fire_append_event')(item, _sa_initiator)
                                                  
def __del(collection, item, _sa_initiator=None):
    """Run del events, may eventually be inlined into decorators."""

    if _sa_initiator is not False and item is not None:
        executor = getattr(collection, '_sa_adapter', None)
        if executor:
            getattr(executor, 'fire_remove_event')(item, _sa_initiator)
    
def _list_decorators():
    """Hand-turned instrumentation wrappers that can decorate any list-like
    class."""
    
    def _tidy(fn):
        try:
            setattr(fn, '_sa_instrumented', True)
            fn.__doc__ = getattr(getattr(list, fn.__name__), '__doc__')
        except:
            raise

    def append(fn):
        def append(self, item, _sa_initiator=None):
            # FIXME: example of fully inlining __set and adapter.fire
            # for critical path
            if _sa_initiator is not False and item is not None:
                executor = getattr(self, '_sa_adapter', None)
                if executor:
                    executor.attr.fire_append_event(executor._owner(),
                                                    item, _sa_initiator)
            fn(self, item)
        _tidy(append)
        return append

    def remove(fn):
        def remove(self, value, _sa_initiator=None):
            fn(self, value)
            __del(self, value, _sa_initiator)
        _tidy(remove)
        return remove

    def insert(fn):
        def insert(self, index, value):
            __set(self, value)
            fn(self, index, value)
        _tidy(insert)
        return insert

    def __setitem__(fn):
        def __setitem__(self, index, value):
            if not isinstance(index, slice):
                existing = self[index]
                if existing is not None:
                    __del(self, existing)
                __set(self, value)
                fn(self, index, value)
            else:
                # slice assignment requires __delitem__, insert, __len__
                if index.stop is None:
                    stop = 0
                elif index.stop < 0:
                    stop = len(self) + index.stop
                else:
                    stop = index.stop
                step = index.step or 1
                rng = range(index.start or 0, stop, step)
                if step == 1:
                    for i in rng:
                        del self[index.start]
                    i = index.start
                    for item in value:
                        self.insert(i, item)
                        i += 1
                else:
                    if len(value) != len(rng):
                        raise ValueError
                    for i, item in zip(rng, value):
                        self.__setitem__(i, item)
        _tidy(__setitem__)
        return __setitem__

    def __delitem__(fn):
        def __delitem__(self, index):
            if not isinstance(index, slice):
                item = self[index]
                __del(self, item)
                fn(self, index)
            else:
                # slice deletion requires __getslice__ and a slice-groking
                # __getitem__ for stepped deletion
                # note: not breaking this into atomic dels
                for item in self[index]:
                    __del(self, item)
                fn(self, index)
        _tidy(__delitem__)
        return __delitem__

    def __setslice__(fn):
        def __setslice__(self, start, end, values):
            for value in self[start:end]:
                __del(self, value)
            for value in values:
                __set(self, value)
            fn(self, start, end, values)
        _tidy(__setslice__)
        return __setslice__
    
    def __delslice__(fn):
        def __delslice__(self, start, end):
            for value in self[start:end]:
                __del(self, value)
            fn(self, start, end)
        _tidy(__delslice__)
        return __delslice__

    def extend(fn):
        def extend(self, iterable):
            for value in iterable:
                self.append(value)
        _tidy(extend)
        return extend
    
    def pop(fn):
        def pop(self, index=-1):
            item = fn(self, index)
            __del(self, item)
            return item
        _tidy(pop)
        return pop

    l = locals().copy()
    l.pop('_tidy')
    return l

def _dict_decorators():
    """Hand-turned instrumentation wrappers that can decorate any dict-like
    mapping class."""

    def _tidy(fn):
        try:
            setattr(fn, '_sa_instrumented', True)
            fn.__doc__ = getattr(getattr(dict, fn.__name__), '__doc__')
        except:
            raise

    Unspecified=object()

    def __setitem__(fn):
        def __setitem__(self, key, value, _sa_initiator=None):
            if key in self:
                __del(self, self[key], _sa_initiator)
            __set(self, value)
            fn(self, key, value)
        _tidy(__setitem__)
        return __setitem__

    def __delitem__(fn):
        def __delitem__(self, key, _sa_initiator=None):
            if key in self:
                __del(self, self[key], _sa_initiator)
            fn(self, key)
        _tidy(__delitem__)
        return __delitem__

    def clear(fn):
        def clear(self):
            for key in self:
                __del(self, self[key])
            fn(self)
        _tidy(clear)
        return clear

    def pop(fn):
        def pop(self, key, default=Unspecified):
            if key in self:
                __del(self, self[key])
            if default is Unspecified:
                return fn(self, key)
            else:
                return fn(self, key, default)
        _tidy(pop)
        return pop

    def popitem(fn):
        def popitem(self):
            item = fn(self)
            __del(self, item[1])
            return item
        _tidy(popitem)
        return popitem

    def setdefault(fn):
        def setdefault(self, key, default=None):
            if key not in self and default is not None:
                __set(self, default)
            return fn(self, key, default)
        _tidy(setdefault)
        return setdefault

    if sys.version_info < (2, 4):
        def update(fn):
            def update(self, other):
                for key in other.keys():
                    self[key] = other[key]
            _tidy(update)
            return update
    else:
        def update(fn):
            def update(self, __other=Unspecified, **kw):
                if __other is not Unspecified:
                    if hasattr(__other, 'keys'):
                        for key in __other.keys():
                            self[key] = __other[key]
                    else:
                        for key, value in __other:
                            self[key] = value
                for key in kw:
                    self[key] = kw[key]
            _tidy(update)
            return update

    l = locals().copy()
    l.pop('_tidy')
    l.pop('Unspecified')
    return l

def _set_decorators():
    """Hand-turned instrumentation wrappers that can decorate any set-like
    sequence class."""

    def _tidy(fn):
        try:
            setattr(fn, '_sa_instrumented', True)
            fn.__doc__ = getattr(getattr(set, fn.__name__), '__doc__')
        except:
            raise

    Unspecified=object()

    def add(fn):
        def add(self, value, _sa_initiator=None):
            __set(self, value, _sa_initiator)
            fn(self, value)
        _tidy(add)
        return add

    def discard(fn):
        def discard(self, value, _sa_initiator=None):
            if value in self:
                __del(self, value, _sa_initiator)
            fn(self, value)
        _tidy(discard)
        return discard

    def remove(fn):
        def remove(self, value, _sa_initiator=None):
            if value in self:
                __del(self, value, _sa_initiator)
            fn(self, value)
        _tidy(remove)
        return remove

    def pop(fn):
        def pop(self):
            item = fn(self)
            __del(self, item)
            return item
        _tidy(pop)
        return pop

    def update(fn):
        def update(self, value):
            for item in value:
                if item not in self:
                    self.add(item)
        _tidy(update)
        return update
    __ior__ = update

    def difference_update(fn):
        def difference_update(self, value):
            for item in value:
                self.discard(item)
        _tidy(difference_update)
        return difference_update
    __isub__ = difference_update

    def intersection_update(fn):
        def intersection_update(self, other):
            want, have = self.intersection(other), sautil.Set(self)
            remove, add = have - want, want - have

            for item in remove:
                self.remove(item)
            for item in add:
                self.add(item)
        _tidy(intersection_update)
        return intersection_update
    __iand__ = intersection_update

    def symmetric_difference_update(fn):
        def symmetric_difference_update(self, other):
            want, have = self.symmetric_difference(other), sautil.Set(self)
            remove, add = have - want, want - have

            for item in remove:
                self.remove(item)
            for item in add:
                self.add(item)
        _tidy(symmetric_difference_update)
        return symmetric_difference_update
    __ixor__ = symmetric_difference_update

    l = locals().copy()
    l.pop('_tidy')
    l.pop('Unspecified')
    return l


class InstrumentedList(list):
    """An instrumented version of the built-in list."""

    __instrumentation__ = {
       'appender': 'append',
       'remover': 'remove',
       'iterator': '__iter__', }

class InstrumentedSet(sautil.Set): 
    """An instrumented version of the built-in set (or Set)."""

    __instrumentation__ = {
       'appender': 'add',
       'remover': 'remove',
       'iterator': '__iter__', }

class InstrumentedDict(dict): 
    """An instrumented version of the built-in dict."""

    __instrumentation__ = {
        'iterator': 'itervalues', }

__canned_instrumentation = {
    list: InstrumentedList,
    sautil.Set: InstrumentedSet,
    dict: InstrumentedDict,
    }

__interfaces = {
    list: { 'appender': 'append',
            'remover':  'remove',
            'iterator': '__iter__',
            '_decorators': _list_decorators(), },
    sautil.Set: { 'appender': 'add',
                  'remover': 'remove',
                  'iterator': '__iter__',
                  '_decorators': _set_decorators(), },
    # < 0.4 compatible naming (almost), deprecated- use decorators instead.
    dict: { 'appender': 'append',
            'remover': 'remove',
            'iterator': 'itervalues',
            '_decorators': _dict_decorators(), },
    # < 0.4 compatible naming, deprecated- use decorators instead.
    None: { 'appender': 'append',
            'remover': 'remove',
            'iterator': 'values', }
    }


class MappedCollection(dict):
    """A basic dictionary-based collection class.

    Extends dict with the minimal bag semantics that collection classes require.
    "append" and "remove" are implemented in terms of a keying function: any
    callable that takes an object and returns an object for use as a dictionary
    key.
    """
    
    def __init__(self, keyfunc):
        self.keyfunc = keyfunc

    def append(self, value, _sa_initiator=None):
        key = self.keyfunc(value)
        self.__setitem__(key, value, _sa_initiator)
    append = collection.internally_instrumented(append)
    append = collection.appender(append)
    
    def remove(self, value, _sa_initiator=None):
        key = self.keyfunc(value)
        # Let self[key] raise if key is not in this collection
        if self[key] != value:
            raise exceptions.InvalidRequestError(
                "Can not remove '%s': collection holds '%s' for key '%s'. "
                "Possible cause: is the MappedCollection key function "
                "based on mutable properties or properties that only obtain "
                "values after flush?" %
                (value, self[key], key))
        self.__delitem__(key, _sa_initiator)
    remove = collection.internally_instrumented(remove)
    remove = collection.remover(remove)
