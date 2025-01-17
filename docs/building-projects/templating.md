# HTML Templating

## What is templating?

If you're building any sort of website, you likely don't want to write HTML from Python strings. Instead, you would rather just render HTML files and keep your Python code seperate.

However, this has a drawback: **you can't put variables into your HTML.** Nearly all Python web frameworks use templating as a solution.

Templating is the use of a template engine to put Python code in your HTML. For a more in-depth explanation, see the [Python Wiki](https://wiki.python.org/moin/Templating).

## Templating API

In View, the main template API entry point is the `template` function. Because this function performs I/O, it is asynchronous.

The only required argument to `template` is the name or path of the template to be used. For example:

```py
from view import new_app, template

app = new_app()

@app.get("/")
async def index():
    return await template("index")  # this refers to index.html

@app.get("/other")
async def other():
    return await template("index.html")  # works the same way

app.run()
```

The most notable difference about view.py's templating API is that parameters are automatically included from your scope (i.e. you don't have to pass them into the call to `template`). If you're against this behavior, you may disable it in the configuration via the `globals` and `locals` settings.

::: view.templates.template

You can override the template engine and settings via the `engine` and `directory` parameters. For example, if the engine was `view`, the below would use `mako`:

```py
from view import new_app, template

app = new_app()

@app.get('/')
async def index():
    return await template("index", engine="mako")

app.run()
```

The following template engines are supported:

- View's built-in engine
- [Jinja](https://jinja.palletsprojects.com/en/3.1.x/)
- [Django Templates](https://docs.djangoproject.com/en/5.0/intro/tutorial03/)
- [Mako](https://www.makotemplates.org/)
- [Chameleon](https://chameleon.readthedocs.io/en/latest/)

## The View Engine

View has it's own built in template engine that is used by default. It's based around the usage of a `<view>` tag, which is more limited, yet pretty to look at.

A `<view>` element can have any of the following attributes:

- `ref`: Can be any Python expression (including variable references).
- `template`: Loads another template in place.
- `if`: Shows the element if the expression is truthy.
- `elif`: Shows the element if the expression is truthy and if the previous `if` or `elif` was falsy.
- `else`: Shows the element if all the previous `if` and `elif`'s were falsy.
- `iter`: May be any iterable expression. An `item` attribute must be present if this attribute is set.
- `item`: Specifies the name for the item in each iteration. Always present when `iter` is set.


### Examples

`ref` can be used to take variables, but may also be used to display any Python expression. For example, if you had defined `hello = "world"`:

```html
<p>Hello, <view ref="hello" /></p>
<p>The length of hello is <view ref="len(hello)" /></p>
```

If you had declared `my_list = [1, 2, 3]`, you could iterate through it like so:

```html
<view iter="my_list" item="i">
    <view ref="i" />
</view>
```
The above would result in `123`

`if`, `elif`, and `else` are only shown if their cases are met. So, for example:

```html
<view if="user.type == 'admin'">
    <view template="admin_panel" />
</view>
<view elif="user.type == 'moderator'">
    <view template="mod_panel" />
</view>
<view else>
    <p>You must be an admin to use the admin panel!</p>
</view>
```

## Using Other Engines

If you would like to use an unsupported engine (or use extra features of a supported engine), you can do one of two things:

- Make a feature request on [GitHub](https://github.com/ZeroIntensity/view.py) requesting for support.
- Manually use it's API to return a response from a route.

For example, if you wanted to customize [Jinja](https://jinja.palletsprojects.com/en/3.1.x/), you shouldn't use View's `template`, but instead just use it manually:

```py
from view import new_app
from jinja2 import Environment

app = new_app()
env = Environment()

@app.get('/')
async def index():
    return env.get_template("mytemplate.html").render()

app.run()
```

## Review

Template engines are used to mix your Python code and HTML. You can use View's `template` function to render a template with one of the supported engines, which are:

- view.py's built-in engine
- [Jinja](https://jinja.palletsprojects.com/en/3.1.x/)
- [Django Templates](https://docs.djangoproject.com/en/5.0/intro/tutorial03/)
- [Mako](https://www.makotemplates.org/)
- [Chameleon](https://chameleon.readthedocs.io/en/latest/)

If you would like to use an unsupported engine, you can make a feature request on [GitHub](https://github.com/ZeroIntensity/view.py/issues), or use it's API manually.
