# Loader

view.py's route loader works by generating a list of `Route` objects, and then passing them to `finalize()`.

`load_fs`

::: view._loader.load_fs

`load_simple`

::: view._loader.load_simple

## Finalizing

This function call method functions on the `App` instance. For example, a `Route` object generated by `@get()` will correspond to `_get` on `App` (technically, it originates from the `ViewApp` class, as it's a C function). It will also call `_format_inputs`, which generates dictionaries that the C loader can understand at runtime.

If a route has inputs that do not have an `Any` type (e.g. in `@app.query("hello", str)` the type is `str`), then it will start a complicated process called type code generation.

`finalize`

::: view._loader.finalize

`_format_inputs`

::: view._loader._format_inputs

## Type Codes

Type codes are easily the most complex part of the loader. It's essentially the converting of types to a tuple that the C ASGI implementation can quickly validate at runtime. The type code system exists to speed up type validation. At runtime, calling lots of `PyObject_IsInstance` functions on the C side can be expensive. view.py solves this by creating a structure in which every type supported has its own type code and information on how to parse it at runtime.

The main entry point for type code generation is `_build_type_codes`:

::: view._loader._build_type_codes

On the Python side, type info should contain four things:
- Type Code
- Type Object (only set when using a `__view_body__` object)
- Children (i.e. the `int` part of `dict[str, int]`)
- Default (only set when typecode is `TYPECODE_CLASSTYPES`)

More on what these mean in a second, but for reference here are the available typecodes:

- `TYPECODE_ANY` (0): Any type may be passed, no further validation is needed.
- `TYPECODE_STR` (1): A string object. Validation will never fail if this is the type, since everything can be stringified.
- `TYPECODE_INT` (2): Any number.
- `TYPECODE_BOOL` (3): Any boolean.
- `TYPECODE_FLOAT` (4): Any number or floating point.
- `TYPECODE_DICT` (5): Any dictionary object. If this has children, they will be validated. Note that dictionary keys can only be strings.
- `TYPECODE_NONE` (6): `None` or `null` is allowed.
- `TYPECODE_CLASS` (7): This is a `__view_body__` object. The second part should contain a Python class, and the children will be typecodes of `TYPECODE_CLASSTYPES`.
- `TYPECODE_CLASSTYPES` (8): This is reserved for children under a `TYPECODE_CLASS` type code. If used otherwise, view.py will crash.
- `TYPECODE_LIST` (9): Any list object. Works the same as dictionaries.

So, how does it work?

To start, a type info list represents all the types available for a certain input. So, if it has one type, only one type is supported (assuming that type is not `Any`, since that can be applied to any type). For example, at the top level a type info list that supports `str | int | bool` looks like the following:

```py
[str_type_info, int_type_info, bool_type_info]`
```

But what does a type info part actually look like? Unless the typecode is `TYPECODE_CLASSTYPES`, it's simply a tuple containing three items:

```py
(typecode_number, typecode_class_object, list_of_children_typecodes)
```

Above, `typecode_number` is one of the typecodes specified above. `typecode_class_object` is the Python class to instantiate at runtime if the typecode is `TYPECODE_CLASS`. If the typecode is something else, then this is `None`. Finally, `list_of_children_typecodes` is for `TYPECODE_CLASS` and `TYPECODE_DICT`. It's a list containing type code parts for use at runtime. This will be explained more in depth later.

So, as an example, the type info for `str` looks like this:

```py
(TYPECODE_STR, None, [])
```

And then when put into the entire list from earlier, it looks like this:

```py
[(TYPECODE_STR, None, []), (TYPECODE_INT, None, []), (TYPECODE_BOOL, None, [])]
```

Now, for those other parameters, let's start with dictionaries. JSON is taken in by queries or HTTP bodies, and in JSON keys can only be strings. So, we already know the first part of the type: `dict[str]`, meaning we don't have to pass any type codes for it since it will always be `str`.

But how do we specify types for the second parameter? That's where the children come in. Let's use the type `dict[str, int]` as an example. Once again, we start with a simple typecode part for dictionaries:

```py
(TYPECODE_DICT, None, [])
```

The above is actually a valid type part, as `[]` is just read as `Any` by view.py, so the above is actually the type info for `dict[str, Any]`. To add a type, we just add the type part to the children part of it, like so:

```py
(TYPECODE_DICT, None, [(TYPECODE_INT, None, [])])`
```

Easy as that! The above is now valid type info for `dict[str, int]`. But what if we want to add unions (i.e. more types)? Just add more type parts to the list, like we did earlier:

```py
(TYPECODE_DICT, None, [(TYPECODE_INT, None, []), (TYPECODE_STR, None, [])])
```

The above is proper for `dict[str, int | str]`. Easy enough so far, right? Now it starts to get really complicated.

Let's dive into how `TYPECODE_CLASS` works. Say we have an object called `TC` with a `__view_body__`:

```py
class TC:
    __view_body__ = {
        "a": str,
    }
```

Ok, let's start small. First, set the type code and object, let's ignore children for now:

```py
(TYPECODE_CLASS, TC, [])
```

The above is technically valid, but not very useful. It would assume that `TC` has no parameters. So how do we add those parameters from the `__view_body__`? This process is called body formatting, and it's main entry point is `_format_body`:

::: view._loader._format_body

But, how does that actually work? Let's take a look at children again:

```py
[]
```

This is simply a list that should contain other type codes, so all we have to do is add some to it. But how do we specify an attribute name?

This is where `TYPECODE_CLASSTYPES` comes in. `TYPECODE_CLASSTYPES` breaks the rules, and can only exist in the children of a `TYPECODE_CLASS`. It expects a tuple containing four items:

```py
(TYPECODE_CLASSTYPES, attribute_name_as_str, allowed_typeinfo, default_value)
```

`attribute_name_as_str` is a string containing the attribute name, so for `a: str` it would be the string `"a"`.
`allowed_typeinfo` works the same as children. It's a list of type parts that say what types are allowed.
`default_value` is the default value in case it wasn't passed with the JSON. If no default is wanted, use the `_NoDefault` object.

Let's build a `TYPECODE_CLASSTYPES` for `a: str`:

```py
(TYPECODE_CLASSTYPES, "a", [(TYPECODE_STR, None, [])], _NoDefault)
```
(TYPECODE_CLASS, TC, [])

Easy enough, right? Now, let's bring it back to the original `TYPECODE_CLASS`:

```py
(TYPECODE_CLASS, TC, [(TYPECODE_CLASSTYPES, "a", [(TYPECODE_STR, None, [])], _NoDefault)])
```

Once again, we go through all this work because trying to mash lots of this information together at runtime is error prone and expensive. The type code system speeds things up by a lot.

Congratulations! You now understand one of the most complicated systems of view.py