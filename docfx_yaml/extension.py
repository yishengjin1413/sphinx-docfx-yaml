# -*- coding: utf-8 -*-
"""
Sphinx DocFX YAML Top-level Extension.

This extension allows you to automagically generate DocFX YAML from your Python Domains.
"""
import os
import inspect
from functools import partial

try:
    from subprocess import getoutput
except ImportError:
    from commands import getoutput

from yaml import safe_dump as dump

from sphinx.util.console import darkgreen, bold
from sphinx.util import ensuredir
from sphinx.errors import ExtensionError

from .utils import transform_node, transform_string
from .settings import API_ROOT
from .monkeypatch import patch_docfields
from .writer import MarkdownWriter as Writer


METHOD = 'method'
FUNCTION = 'function'
MODULE = 'module'
CLASS = 'class'
EXCEPTION = 'exception'
ATTRIBUTE = 'attribute'

# We need to map the Python type names to what DocFX is expecting
TYPE_MAPPING = {
    METHOD: 'Method',
    FUNCTION: 'Method',
    MODULE: 'Namespace',
    CLASS: 'Class',
    EXCEPTION: 'Class',  # Hack this for now
    ATTRIBUTE: 'Property',  # Ditto
}


def build_init(app):
    """
    Set up environment data
    """
    if not app.config.docfx_yaml_output:
        raise ExtensionError('You must configure an docfx_yaml_output setting')

    app.env.docfx_yaml_modules = {}
    app.env.docfx_yaml_classes = {}
    app.env.docfx_module_data = {}

    remote = getoutput('git remote -v')

    try:
        app.env.docfx_remote = remote.split('\t')[1].split(' ')[0]
    except Exception:
        app.env.docfx_remote = None
    try:
        app.env.docfx_branch = getoutput('git rev-parse --abbrev-ref HEAD').strip()
    except Exception:
        app.env.docfx_branch = None

    try:
        app.env.docfx_root = getoutput('git rev-parse --show-toplevel').strip()
    except Exception:
        app.env.docfx_root = None

    patch_docfields(app)
    app.docfx_writer = Writer(app.builder)
    app.docfx_transform_node = partial(transform_node, app)
    app.docfx_transform_string = partial(transform_string, app)


def _get_cls_module(_type, name):
    """
    Get the class and module name for an object

    .. _sending:

    Foo

    """
    cls = None
    if _type in ['function', 'exception']:
        module = '.'.join(name.split('.')[:-1])
    elif _type in ['method', 'attribute']:
        cls = '.'.join(name.split('.')[:-1])
        module = '.'.join(name.split('.')[:-2])
    elif _type in ['class']:
        cls = name
        module = '.'.join(name.split('.')[:-1])
    elif _type in ['module']:
        module = name
    else:
        return (None, None)
    return (cls, module)


def _create_reference(datam, parent, is_external=False):
    return {
        'uid': datam['uid'],
        'parent': parent,
        'isExternal': is_external,
        'name': datam['name'],
        'fullName': datam['fullName'],
    }


def _create_datam(app, cls, module, name, _type, obj, lines=[]):
    """
    Build the data structure for a autodoc class
    """
    try:
        mapped_type = TYPE_MAPPING[_type]
    except TypeError:
        print('Invalid Type Mapping: %s' % _type)
        mapped_type = _type

    short_name = name.split('.')[-1]
    summary = app.docfx_transform_string('\n'.join(lines))
    args = []
    try:
        if _type in [METHOD, FUNCTION]:
            argspec = inspect.getargspec(obj)
            for arg in argspec.args:
                args.append({'id': arg})
            if argspec.defaults:
                for count, default in enumerate(argspec.defaults):
                    cut_count = len(argspec.defaults)
                    # Match the defaults with the count
                    args[len(args) - 1 - cut_count - 1 - count]['defaultValue'] = str(default)
    except Exception:
        print("Can't get argspec for {}: {}".format(type(obj), name))

    try:
        full_path = inspect.getsourcefile(obj)
        # Sub git repo path
        path = full_path.replace(app.env.docfx_root, '')
        # Support global file imports, if it's installed already
        import_path = os.path.dirname(inspect.getfile(os))
        path = path.replace(os.path.join(import_path, 'site-packages'), '')
        path = path.replace(import_path, '')
        # Make relative
        path = path.replace('/', '', 1)
        start_line = inspect.getsourcelines(obj)[1]
    except (TypeError, OSError):
        print("Can't inspect type {}: {}".format(type(obj), name))
        path = None
        start_line = None

    datam = {
        'module': module,
        'uid': name,
        'type': mapped_type,
        '_type': _type,
        'name': short_name,
        'fullName': name,
        'source': {
            'remote': {
                'path': path,
                'branch': app.env.docfx_branch,
                'repo': app.env.docfx_remote,
            },
            'id': short_name,
            'path': path,
            'startLine': start_line,
        },
        'langs': ['python'],
    }

    if summary:
        datam['summary'] = summary
    if args:
        datam['syntax'] = {
            'parameters': args,
        }

    if cls:
        datam['class'] = cls
    if _type in ['class', 'module']:
        datam['children'] = []
        datam['references'] = []

    return datam


def _fullname(obj):
    """
    Get the fullname from a Python object
    """
    return obj.__module__ + "." + obj.__name__


def process_docstring(app, _type, name, obj, options, lines):
    """
    This function takes the docstring and indexes it into memory.
    """
    cls, module = _get_cls_module(_type, name)
    if not module:
        print('Unknown Type: %s' % _type)
        return None

    datam = _create_datam(app, cls, module, name, _type, obj, lines)

    if _type == MODULE:
        if module not in app.env.docfx_yaml_modules:
            app.env.docfx_yaml_modules[module] = [datam]
        else:
            app.env.docfx_yaml_modules[module].append(datam)

    if _type == CLASS:
        if cls not in app.env.docfx_yaml_classes:
            app.env.docfx_yaml_classes[cls] = [datam]
        else:
            app.env.docfx_yaml_classes[cls].append(datam)

    insert_inheritance(app, _type, obj, datam)

    insert_children_on_module(app, _type, datam)
    insert_children_on_class(app, _type, datam)


def collect_inheritance(base, to_add):
    for new_base in base.__bases__:
        new_add = []
        new_add.append(_fullname(new_base))
        collect_inheritance(new_base, new_add)
        to_add.append(new_add)


def insert_inheritance(app, _type, obj, datam):
    if hasattr(obj, '__bases__'):
        if 'inheritance' not in datam:
            datam['inheritance'] = []
        for base in obj.__bases__:
            to_add = [_fullname(base)]
            collect_inheritance(base, to_add)
            datam['inheritance'].append(to_add)


def insert_children_on_module(app, _type, datam):
    """
    Insert children of a specific module
    """

    if 'module' not in datam or datam['module'] not in app.env.docfx_yaml_modules:
        return
    insert_module = app.env.docfx_yaml_modules[datam['module']]
    # Find the module which the datam belongs to
    for obj in insert_module:
        # Add standardlone function to global class
        if _type in ['function'] and \
                obj['_type'] == 'module' and \
                obj['module'] == datam['module']:
            obj['children'].append(datam['uid'])
            insert_module.append(datam)
            obj['references'].append(_create_reference(datam, parent=obj['uid']))
            break
        # Add classes & exceptions to module
        if _type in ['class', 'exception'] and \
                obj['_type'] == 'module' and \
                obj['module'] == datam['module']:
            obj['children'].append(datam['uid'])
            obj['references'].append(_create_reference(datam, parent=obj['uid']))
            break


def insert_children_on_class(app, _type, datam):
    """
    Insert children of a specific class
    """
    if 'class' not in datam:
        return

    insert_class = app.env.docfx_yaml_classes[datam['class']]
    # Find the class which the datam belongs to
    for obj in insert_class:
        if obj['_type'] != CLASS:
            continue
        # Add methods & attributes to class
        if _type in ['method', 'attribute'] and \
                obj['class'] == datam['class']:
            obj['children'].append(datam['uid'])
            obj['references'].append(_create_reference(datam, parent=obj['uid']))
            insert_class.append(datam)


def build_finished(app, exception):
    """
    Output YAML on the file system.
    """

    normalized_output = os.path.normpath(os.path.join(
        app.builder.outdir,  # Output Directory for Builder
        API_ROOT,
    ))
    ensuredir(normalized_output)

    toc_yaml = []

    iter_data = []
    iter_data.append(app.env.docfx_yaml_modules)
    iter_data.append(app.env.docfx_yaml_classes)

    for data_set in iter_data:
        for filename, yaml_data in iter(sorted(data_set.items())):
            if not filename:
                # Skip objects without a module
                continue

            references = []

            # Merge module data with class data
            for obj in yaml_data:
                if obj['uid'] in app.env.docfx_module_data:
                    if 'syntax' not in obj:
                        obj['syntax'] = {}
                    merged_params = []
                    if 'parameters' in app.env.docfx_module_data[obj['uid']]:
                        arg_params = obj['syntax'].get('parameters', [])
                        doc_params = app.env.docfx_module_data[obj['uid']].get('parameters', [])
                        if arg_params and doc_params:
                            if len(arg_params) - len(doc_params) > 1:
                                app.warn(
                                    "Documented params don't match size of params:"
                                    " {}".format(obj['uid']))
                            if len(arg_params) - len(doc_params) == 1:
                                # Support having `self` as an arg param, but not documented
                                merged_params = [arg_params[0]]
                                arg_params = arg_params[1:]
                            for args, docs in zip(arg_params, doc_params):
                                args.update(docs)
                                merged_params.append(args)
                    obj['syntax'].update(app.env.docfx_module_data[obj['uid']])
                    if merged_params:
                        obj['syntax']['parameters'] = merged_params

                    # Raise up summary
                    if 'summary' in obj['syntax'] and obj['syntax']['summary']:
                        obj['summary'] = obj['syntax'].pop('summary')
                if 'references' in obj:
                    references.extend(obj.pop('references'))

            # Output file
            out_file = os.path.join(normalized_output, '%s.yml' % filename)
            ensuredir(os.path.dirname(out_file))
            if app.verbosity >= 1:
                app.info(bold('[docfx_yaml] ') + darkgreen('Outputting %s' % filename))
            with open(out_file, 'w') as out_file_obj:
                out_file_obj.write('#YamlMime:PythonReference\n')
                dump(
                    {
                        'items': yaml_data,
                        'references': references,
                        'api_name': [],  # Hack around docfx YAML
                    },
                    out_file_obj,
                    default_flow_style=False
                )
            if filename.count('.') > 1:
                second_level = '.'.join(filename.split('.')[:2])
                for module in toc_yaml:
                    if module['name'] == second_level:
                        if 'items' not in module:
                            module['items'] = []
                        module['items'].append({'name': filename, 'href': '%s.yml' % filename})
                        break
                else:
                    print('No second level module found: {}'.format(second_level))
            else:
                toc_yaml.append({'name': filename, 'href': '%s.yml' % filename})

    toc_file = os.path.join(normalized_output, 'toc.yml')
    with open(toc_file, 'w') as writable:
        writable.write(
            dump(
                toc_yaml,
                default_flow_style=False,
            )
        )


def setup(app):
    """
    Plugin init for our Sphinx extension.

    Args:
        app (Application): The Sphinx application
           instance is destructed

    """
    app.connect('builder-inited', build_init)
    app.connect('autodoc-process-docstring', process_docstring)
    app.connect('build-finished', build_finished)
    app.add_config_value('docfx_yaml_output', API_ROOT, 'html')

    # For testing doctree parsing
    # app.connect('doctree-resolved', doctree_resolved)
