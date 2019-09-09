import operator
from collections import OrderedDict
from collections import namedtuple
from functools import partial

from elasticsearch import ElasticsearchException

from .compat import Iterable
from .compat import Mapping
from .compat import string_types
from .document import DOC_TYPE_FIELD_NAME
from .document import DOC_TYPE_ID_DELIMITER
from .document import DOC_TYPE_PARENT_DELIMITER
from .document import Document
from .document import DynamicDocument
from .expression import Bool
from .expression import Exists
from .expression import Filtered
from .expression import FunctionScore
from .expression import HighlightedField
from .result import BulkResult
from .result import CountResult
from .result import DeleteByQueryResult
from .result import DeleteResult
from .result import ExistsResult
from .result import PutMappingResult
from .result import SearchResult
from .search import BaseSearchQuery
from .search import SearchQueryContext
from .types import ValidationError
from .util import collect_doc_classes


BOOL_OPERATOR_NAMES = {
    operator.and_: 'and',
    operator.or_: 'or',
}

BOOL_OPERATORS_MAP = {
    operator.and_: Bool.must,
    operator.or_: Bool.should,
}

DEFAULT_DOC_TYPE = '_doc'

ESVersion = namedtuple('ESVersion', ['major', 'minor', 'patch'])

ElasticsearchFeatures = namedtuple(
    'ExpressionFeatures',
    [
        'supports_old_boolean_queries',
        'supports_missing_query',
        'supports_parent_id_query',
        'supports_bool_filter',
        'supports_search_exists_api',
        'supports_mapping_types',
        'stored_fields_param',
    ]
)


class CompilationError(Exception):
    pass


class MultiSearchError(ElasticsearchException):
    pass


def _is_emulate_doc_types_mode(features, doc_cls):
    return (
        not features.supports_mapping_types and
        doc_cls.get_doc_type() and
        doc_cls.has_parent_doc_cls()
    )


def _doc_type_and_id(doc_type, doc_id):
    return '{}{}{}'.format(doc_type, DOC_TYPE_ID_DELIMITER, doc_id)


def _doc_type_field_name(doc_type):
    return '{}{}{}'.format(
        DOC_TYPE_FIELD_NAME, DOC_TYPE_PARENT_DELIMITER, doc_type
    )


class Compiled(object):
    compiler = None
    features = None

    def __init__(self, expression, params=None):
        self.expression = expression
        self.body = self.visit(expression)
        self.params = self.prepare_params(params or {})

    def prepare_params(self, params):
        return params

    def visit(self, expr, **kwargs):
        visit_name = None
        if hasattr(expr, '__visit_name__'):
            visit_name = expr.__visit_name__

        if visit_name:
            visit_func = getattr(self, 'visit_{}'.format(visit_name))
            return visit_func(expr, **kwargs)

        if isinstance(expr, dict):
            return self.visit_dict(expr)

        if isinstance(expr, (list, tuple)):
            return self.visit_list(expr)

        return expr

    def visit_params(self, params):
        res = {}
        for k, v in params.items():
            res[self.visit(k)] = self.visit(v)
        return res

    def visit_dict(self, dct):
        return {self.visit(k): self.visit(v) for k, v in dct.items()}

    def visit_list(self, lst):
        return [self.visit(v) for v in lst]


class CompiledEndpoint(Compiled):
    def process_result(self, raw_result):
        raise NotImplementedError


class CompiledExpression(Compiled):
    def __init__(self, expr, params=None, doc_classes=None):
        self.doc_classes = doc_classes
        super(CompiledExpression, self).__init__(expr, params)

    def visit_literal(self, expr):
        return expr.obj

    def visit_field(self, field):
        return field._name

    def visit_mapping_field(self, field):
        return field._name

    def visit_attributed_field(self, field):
        return field._field._name

    def visit_boost_expression(self, expr):
        return '{}^{}'.format(self.visit(expr.expr), self.visit(expr.weight))

    def visit_query_expression(self, expr):
        return {
            expr.__query_name__: self.visit(expr.params)
        }

    def visit_field_query(self, expr):
        if expr.params:
            params = {expr.__query_key__: self.visit(expr.query)}
            params.update(expr.params)
            return {
                expr.__query_name__: {
                    self.visit(expr.field): params
                }
            }
        else:
            return {
                expr.__query_name__: {
                    self.visit(expr.field): self.visit(expr.query)
                }
            }

    def visit_range(self, expr):
        field_params = {
            self.visit(expr.field): self.visit(expr.params)
        }
        return {
            'range': dict(self.visit(expr.range_params), **field_params)
        }

    def visit_terms(self, expr):
        params = {self.visit(expr.field): self.visit(expr.terms)}
        params.update(self.visit(expr.params))
        return {
            'terms': params
        }

    def visit_missing(self, expr):
        if self.features.supports_missing_query:
            return {
                'missing': self.visit(expr.params)
            }
        return self.visit(
            Bool.must_not(Exists(**expr.params))
        )

    def visit_multi_match(self, expr):
        params = {
            'query': self.visit(expr.query),
            'fields': [self.visit(f) for f in expr.fields],
        }
        params.update(self.visit(expr.params))
        return {
            'multi_match': params
        }

    def visit_match_all(self, expr):
        return {'match_all': self.visit(expr.params)}

    def visit_query(self, expr):
        params = {
            'query': self.visit(expr.query)
        }
        if expr.params:
            params.update(self.visit(expr.params))
            return {
                'fquery': params
            }
        return params

    def visit_boolean_expression(self, expr):
        if not self.features.supports_old_boolean_queries:
            return self.visit(
                BOOL_OPERATORS_MAP[expr.operator](*expr.expressions)
            )
        if expr.params:
            params = {
                'filters': [self.visit(e) for e in expr.expressions]
            }
            params.update(self.visit(expr.params))
        else:
            params = [self.visit(e) for e in expr.expressions]
        return {
            BOOL_OPERATOR_NAMES[expr.operator]: params
        }

    def visit_not(self, expr):
        if not self.features.supports_old_boolean_queries:
            return self.visit(Bool.must_not(expr))
        if expr.params:
            params = {
                'filter': self.visit(expr.expr)
            }
            params.update(self.visit(expr.params))
        else:
            params = self.visit(expr.expr)
        return {
            'not': params
        }

    def visit_sort(self, expr):
        if expr.params:
            params = {'order': self.visit(expr.order)}
            params.update(self.visit(expr.params))
            return {
                self.visit(expr.expr): params
            }
        elif expr.order:
            return {
                self.visit(expr.expr): self.visit(expr.order)
            }
        else:
            return self.visit(expr.expr)

    def visit_agg(self, agg):
        return {
            agg.__agg_name__: self.visit(agg.params)
        }

    def visit_bucket_agg(self, agg):
        params = {
            agg.__agg_name__: self.visit(agg.params)
        }
        if agg._aggregations:
            params['aggregations'] = self.visit(agg._aggregations)
        return params

    def visit_filter_agg(self, agg):
        params = self.visit_bucket_agg(agg)
        params[agg.__agg_name__] = self.visit(agg.filter)
        return params

    def visit_source(self, expr):
        if expr.include or expr.exclude:
            params = {}
            if expr.include:
                params['include'] = self.visit(expr.include)
            if expr.exclude:
                params['exclude'] = self.visit(expr.exclude)
            return params
        if isinstance(expr.fields, bool):
            return expr.fields
        return [self.visit(f) for f in expr.fields]

    def visit_query_rescorer(self, rescorer):
        return {'query': self.visit(rescorer.params)}

    def visit_rescore(self, rescore):
        params = self.visit(rescore.rescorer)
        if rescore.window_size is not None:
            params['window_size'] = rescore.window_size
        return params

    def visit_highlighted_field(self, hf):
        return {
            self.visit(hf.field): self.visit(hf.params)
        }

    def visit_highlight(self, highlight):
        params = self.visit(highlight.params)
        if highlight.fields:
            if isinstance(highlight.fields, Mapping):
                compiled_fields = {}
                for f, options in highlight.fields.items():
                    compiled_fields[self.visit(f)] = self.visit(options)
                params['fields'] = compiled_fields
            elif isinstance(highlight.fields, Iterable):
                compiled_fields = []
                for f in highlight.fields:
                    if isinstance(f, (HighlightedField, Mapping)):
                        compiled_fields.append(self.visit(f))
                    else:
                        compiled_fields.append({self.visit(f): {}})
                params['fields'] = compiled_fields
        return params

    def visit_ids(self, expr):
        params = self.visit(expr.params)

        if (
                isinstance(expr.type, type) and
                issubclass(expr.type, Document) and
                _is_emulate_doc_types_mode(self.features, expr.type)
        ):
            params['values'] = [
                _doc_type_and_id(expr.type.__doc_type__, v)
                for v in expr.values
            ]
        elif (
                self.doc_classes and
                any(map(
                    partial(_is_emulate_doc_types_mode, self.features),
                    self.doc_classes
                ))
        ):
            ids = []
            for doc_cls in self.doc_classes:
                if _is_emulate_doc_types_mode(self.features, doc_cls):
                    ids.extend(
                        _doc_type_and_id(doc_cls.__doc_type__, v)
                        for v in expr.values
                    )
            params['values'] = ids
        else:
            params['values'] = expr.values
            if expr.type:
                doc_type = getattr(expr.type, '__doc_type__', None)
                if doc_type:
                    params['type'] = doc_type
                else:
                    params['type'] = self.visit(expr.type)

        return {
            'ids': params
        }

    def visit_parent_id(self, expr):
        if not self.features.supports_parent_id_query:
            raise CompilationError(
                'Elasticsearch before 5.x does not have support for '
                'parent_id query'
            )

        if _is_emulate_doc_types_mode(self.features, expr.child_type):
            parent_id = _doc_type_and_id(
                expr.child_type.__parent__.__doc_type__,
                expr.parent_id
            )
        else:
            parent_id = expr.parent_id

        child_type = expr.child_type
        if hasattr(child_type, '__doc_type__'):
            child_type = child_type.__doc_type__
        if not child_type:
            raise CompilationError(
                "Cannot detect child type, specify 'child_type' argument"
            )

        return {'parent_id': {'type': child_type, 'id': parent_id}}

    def visit_has_parent(self, expr):
        params = self.visit(expr.params)
        parent_type = expr.parent_type
        if hasattr(parent_type, '__doc_type__'):
            parent_type = parent_type.__doc_type__
        if not parent_type:
            parent_doc_classes = collect_doc_classes(expr.params)
            if len(parent_doc_classes) == 1:
                parent_type = next(iter(parent_doc_classes)).__doc_type__
            elif len(parent_doc_classes) > 1:
                raise CompilationError(
                    'Too many candidates for parent type, '
                    'should be only one'
                )
            else:
                raise CompilationError(
                    'Cannot detect parent type, '
                    'specify \'parent_type\' argument'
                )
        params['parent_type'] = parent_type
        return {'has_parent': params}

    def visit_has_child(self, expr):
        params = self.visit(expr.params)
        child_type = expr.type
        if hasattr(child_type, '__doc_type__'):
            child_type = child_type.__doc_type__
        if not child_type:
            child_doc_classes = expr.params._collect_doc_classes()
            if len(child_doc_classes) == 1:
                child_type = next(iter(child_doc_classes)).__doc_type__
            elif len(child_doc_classes) > 1:
                raise CompilationError(
                    'Too many candidates for child type, '
                    'should be only one'
                )
            else:
                raise CompilationError(
                    'Cannot detect child type, '
                    'specify \'type\' argument'
                )
        params['type'] = child_type
        return {'has_child': params}

    def visit_script(self, script):
        # TODO Wrap into a dictionary with 'script' key
        return self.visit(script.params)

    def visit_function(self, func):
        params = {func.__func_name__: self.visit(func.params)}
        if func.filter:
            params['filter'] = self.visit(func.filter)
        if func.weight is not None:
            params['weight'] = self.visit(func.weight)
        return params

    def visit_weight_function(self, func):
        params = {func.__func_name__: func.weight}
        if func.filter:
            params['filter'] = self.visit(func.filter)
        return params

    def visit_decay_function(self, func):
        params = {func.__func_name__: {
            self.visit(func.field): self.visit(func.decay_params)
        }}
        if func.params:
            params[func.__func_name__].update(self.visit(func.params))
        if func.filter:
            params['filter'] = self.visit(func.filter)
        return params


class CompiledSearchQuery(CompiledExpression, CompiledEndpoint):
    features = None

    def __init__(self, query, params=None):
        if isinstance(query, BaseSearchQuery):
            expression = query.get_context()
            doc_classes = expression.doc_classes
        elif query is None:
            expression = None
            doc_classes = None
        else:
            expression = {
                'query': query,
            }
            doc_classes = collect_doc_classes(query)
        super(CompiledSearchQuery, self).__init__(
            expression, params, doc_classes=doc_classes
        )

    def api_method(self, client):
        return client.search

    def prepare_params(self, params):
        if isinstance(self.expression, SearchQueryContext):
            search_params = dict(self.expression.search_params)
            search_params.update(params)
            if self.expression.doc_type:
                search_params['doc_type'] = self.expression.doc_type
        else:
            search_params = params
        return self._patch_doc_type(search_params)

    def process_result(self, raw_result):
        return SearchResult(
            raw_result,
            aggregations=self.expression.aggregations,
            doc_cls=self.expression.doc_classes,
            instance_mapper=self.expression.instance_mapper,
        )

    @classmethod
    def get_query(cls, query_context, wrap_function_score=True):
        q = query_context.q
        if wrap_function_score:
            for (functions, params) in reversed(
                    # Without wrapping in list it fails on Python 3.4
                    list(query_context.function_scores.values())
            ):
                if not functions:
                    continue
                q = FunctionScore(
                    query=q,
                    functions=functions,
                    **params
                )
        return q

    @classmethod
    def get_filtered_query(cls, query_context, wrap_function_score=True):
        q = cls.get_query(
            query_context, wrap_function_score=wrap_function_score
        )
        if query_context.filters:
            filter_clauses = list(query_context.iter_filters())
            if cls.features.supports_bool_filter:
                return Bool(must=q, filter=Bool.must(*filter_clauses))
            return Filtered(
                query=q, filter=Bool.must(*filter_clauses)
            )
        return q

    @classmethod
    def get_post_filter(cls, query_context):
        post_filters = list(query_context.iter_post_filters())
        if post_filters:
            return Bool.must(*post_filters)

    def visit_search_query_context(self, query_ctx):
        params = {}

        q = self.get_filtered_query(query_ctx)
        if q is not None:
            params['query'] = self.visit(q)

        post_filter = self.get_post_filter(query_ctx)
        if post_filter:
            params['post_filter'] = self.visit(post_filter)

        if query_ctx.order_by:
            params['sort'] = self.visit(query_ctx.order_by)
        if query_ctx.source:
            params['_source'] = self.visit(query_ctx.source)
        if query_ctx.fields is not None:
            stored_fields_param = self.features.stored_fields_param
            if query_ctx.fields is True:
                params[stored_fields_param] = '*'
            elif query_ctx.fields is False:
                pass
            else:
                params[stored_fields_param] = self.visit(
                    query_ctx.fields
                )
        if query_ctx.aggregations:
            params['aggregations'] = self.visit(
                query_ctx.aggregations
            )
        if query_ctx.limit is not None:
            params['size'] = query_ctx.limit
        if query_ctx.offset is not None:
            params['from'] = query_ctx.offset
        if query_ctx.min_score is not None:
            params['min_score'] = query_ctx.min_score
        if query_ctx.rescores:
            params['rescore'] = self.visit(query_ctx.rescores)
        if query_ctx.suggest:
            params['suggest'] = self.visit(query_ctx.suggest)
        if query_ctx.highlight:
            params['highlight'] = self.visit(query_ctx.highlight)
        if query_ctx.script_fields:
            params['script_fields'] = self.visit(
                query_ctx.script_fields
            )
        return self._patch_docvalue_fields(params)

    def _patch_docvalue_fields(self, params):
        if self.features.supports_mapping_types:
            return params

        docvalue_fields = params.get('docvalue_fields')
        # Wildcards in docvalue_fields aren't supported by top_hits aggregation
        # doc_type_field = '{}*'.format(DOC_TYPE_FIELD_NAME)
        parent_doc_types = set(
            doc_cls.__doc_type__
            for doc_cls in self.doc_classes
            if doc_cls.get_doc_type() and doc_cls.has_parent_doc_cls()
        )
        if not parent_doc_types:
            return params

        doc_type_fields = [DOC_TYPE_FIELD_NAME]
        for doc_type in parent_doc_types:
            doc_type_fields.append(
                _doc_type_field_name(doc_type)
            )
        doc_type_fields.sort()
        if not docvalue_fields:
            params['docvalue_fields'] = doc_type_fields
        elif isinstance(docvalue_fields, string_types):
            params['docvalue_fields'] = [docvalue_fields] + doc_type_fields
        elif isinstance(docvalue_fields, list):
            docvalue_fields.extend(doc_type_fields)
        return params

    def _patch_doc_type(self, search_params):
        if self.features.supports_mapping_types:
            return search_params

        should_use_default_type = self.doc_classes and any(map(
            lambda doc_cls: doc_cls.has_parent_doc_cls(),
            self.doc_classes
        ))
        if should_use_default_type and 'doc_type' in search_params:
            search_params['doc_type'] = DEFAULT_DOC_TYPE
        return search_params


class CompiledScroll(CompiledEndpoint):
    def __init__(self, params, doc_cls=None, instance_mapper=None):
        self.doc_cls = doc_cls
        self.instance_mapper = instance_mapper
        super(CompiledScroll, self).__init__(None, params)

    def api_method(self, client):
        return client.scroll

    def process_result(self, raw_result):
        return SearchResult(
            raw_result,
            doc_cls=self.doc_cls,
            instance_mapper=self.instance_mapper,
        )


class CompiledScalarQuery(CompiledSearchQuery):
    def visit_search_query_context(self, query_ctx):
        params = {}

        q = self.get_filtered_query(query_ctx)
        if q is not None:
            params['query'] = self.visit(q)

        post_filter = self.get_post_filter(query_ctx)
        if post_filter:
            params['post_filter'] = self.visit(post_filter)

        if query_ctx.min_score is not None:
            params['min_score'] = query_ctx.min_score
        return params


class CompiledCountQuery(CompiledScalarQuery):
    def api_method(self, client):
        return client.count

    def process_result(self, raw_result):
        return CountResult(raw_result)


class CompiledExistsQuery(CompiledScalarQuery):
    def __init__(self, query, params=None):
        super(CompiledExistsQuery, self).__init__(query, params)
        if not self.features.supports_search_exists_api:
            if self.body is None:
                self.body = {}
            self.body['size'] = 0
            self.body['terminate_after'] = 1

    def api_method(self, client):
        if self.features.supports_search_exists_api:
            return client.exists
        else:
            return client.search

    def process_result(self, raw_result):
        if self.features.supports_search_exists_api:
            return ExistsResult(raw_result)
        return ExistsResult({
            'exists': SearchResult(raw_result).total >= 1
        })


class CompiledDeleteByQuery(CompiledScalarQuery):
    def api_method(self, client):
        return client.delete_by_query

    def process_result(self, raw_result):
        return DeleteByQueryResult(raw_result)


class CompiledMultiSearch(CompiledEndpoint):
    compiled_search = None

    class _MultiQueries(object):
        __visit_name__ = 'multi_queries'

        def __init__(self, queries):
            self.queries = queries

        def __iter__(self):
            return iter(self.queries)

    def __init__(self, queries, params=None, raise_on_error=False):
        self.raise_on_error = raise_on_error
        self.compiled_queries = []
        super(CompiledMultiSearch, self).__init__(
            self._MultiQueries(queries), params
        )

    def api_method(self, client):
        return client.msearch

    def visit_multi_queries(self, expr):
        body = []
        for q in expr.queries:
            compiled_query = self.compiled_search(q)
            self.compiled_queries.append(compiled_query)
            params = compiled_query.params
            if isinstance(compiled_query.expression, SearchQueryContext):
                index = compiled_query.expression.index
                if index:
                    params['index'] = index.get_name()
            if 'doc_type' in params:
                params['type'] = params.pop('doc_type')
            body.append(params)
            body.append(compiled_query.body)
        return body

    def process_result(self, raw_result):
        errors = []
        for raw, query, compiled_query in zip(
                raw_result['responses'], self.expression, self.compiled_queries
        ):
            result = compiled_query.process_result(raw)
            query._cached_result = result
            if result.error:
                errors.append(result.error)

        if self.raise_on_error and errors:
            if len(errors) == 1:
                error_msg = '1 query was failed'
            else:
                error_msg = '{} queries were failed'.format(len(errors))
            raise MultiSearchError(error_msg, errors)

        return [q.get_result() for q in self.expression]


class CompiledPutMapping(CompiledEndpoint):
    class _MultipleMappings(object):
        __visit_name__ = 'multiple_mappings'

        def __init__(self, mappings):
            self.mappings = mappings

    def __init__(self, doc_cls_or_mapping, params=None, ordered=False):
        self._dict_type = OrderedDict if ordered else dict
        self._dynamic_templates = []
        if isinstance(doc_cls_or_mapping, list):
            doc_cls_or_mapping = self._MultipleMappings(doc_cls_or_mapping)
        super(CompiledPutMapping, self).__init__(doc_cls_or_mapping, params)

    def api_method(self, client):
        return client.indices.put_mapping

    def prepare_params(self, params):
        if params.get('doc_type') is None:
            params['doc_type'] = getattr(
                self.expression, '__doc_type__', None
            )
        return params

    def process_result(self, raw_result):
        return PutMappingResult(raw_result)

    def _visit_dynamic_field(self, field):
        self._dynamic_templates.append(
            {
                field._field._name: {
                    'path_match': field._field._name,
                    'mapping': next(iter(self.visit(field).values()))
                }
            }
        )

    def visit_field(self, field):
        field_type = field.get_type()
        mapping = self._dict_type()
        mapping['type'] = field_type.__visit_name__

        if field_type.doc_cls:
            mapping.update(field_type.doc_cls.__mapping_options__)
            mapping['properties'] = self.visit(field_type.doc_cls.user_fields)

        if field._fields:
            if isinstance(field._fields, Mapping):
                for subfield_name, subfield in field._fields.items():
                    subfield_name = subfield.get_name() or subfield_name
                    subfield_mapping = next(iter(
                        self.visit(subfield).values()
                    ))
                    mapping.setdefault('fields', {}) \
                        .update({subfield_name: subfield_mapping})
            else:
                for subfield in field._fields:
                    mapping.setdefault('fields', {}) \
                        .update(self.visit(subfield))

        mapping.update(field._mapping_options)

        return {
            field.get_name(): mapping
        }

    def visit_mapping_field(self, field):
        mapping = self._dict_type()
        if field._mapping_options:
            mapping[field.get_name()] = field._mapping_options
        return mapping

    def visit_attributed_field(self, field):
        for f in field.dynamic_fields:
            self._visit_dynamic_field(f)
        return self.visit(field.get_field())

    def visit_ordered_attributes(self, attrs):
        mapping = self._dict_type()
        for f in attrs:
            mapping.update(self.visit(f))
        return mapping

    @staticmethod
    def _get_parent_doc_type(doc_cls):
        doc_type = doc_cls.get_doc_type()
        if not doc_type:
            return None
        parent_doc_cls = doc_cls.get_parent_doc_cls()
        if parent_doc_cls is None:
            return None
        return parent_doc_cls.get_doc_type()

    @staticmethod
    def _merge_properties(mappings, properties):
        mapping_properties = mappings.setdefault('properties', {})
        for name, value in properties.items():
            existing_value = mapping_properties.get(name)
            if existing_value is not None and value != existing_value:
                raise ValueError('Conflicting mapping properties: {}'.format(
                    name
                ))
            mapping_properties[name] = value

    def visit_multiple_mappings(self, multiple_mappings):
        mappings = {}
        relations = {}
        for mapping_or_doc_cls in multiple_mappings.mappings:
            if issubclass(mapping_or_doc_cls, Document):
                doc_type = mapping_or_doc_cls.get_doc_type()
                parent_doc_type = self._get_parent_doc_type(mapping_or_doc_cls)
                if doc_type and parent_doc_type:
                    relations.setdefault(parent_doc_type, []).append(doc_type)

            mapping = self.visit(mapping_or_doc_cls)
            if self.features.supports_mapping_types:
                mappings.update(mapping)
            else:
                self._merge_properties(mappings, mapping['properties'])

        if not self.features.supports_mapping_types and relations:
            doc_type_property = mappings['properties'][DOC_TYPE_FIELD_NAME]
            doc_type_property['relations'] = relations

        return mappings

    def visit_document(self, doc_cls):
        mapping = self._dict_type()
        mapping.update(doc_cls.__mapping_options__)
        mapping.update(self.visit(doc_cls.mapping_fields))
        properties = self.visit(doc_cls.user_fields)
        if _is_emulate_doc_types_mode(self.features, doc_cls):
            properties[DOC_TYPE_FIELD_NAME] = {'type': 'join'}
        mapping['properties'] = properties
        for f in doc_cls.dynamic_fields:
            self._visit_dynamic_field(f)
        if self._dynamic_templates:
            mapping['dynamic_templates'] = self._dynamic_templates
        if self.features.supports_mapping_types:
            return {
                doc_cls.__doc_type__: mapping
            }
        else:
            return mapping


class CompiledGet(CompiledEndpoint):
    META_FIELDS = ('_id', '_type', '_routing', '_parent', '_version')

    def __init__(self, doc_or_id, params=None, doc_cls=None):
        self.doc_or_id = doc_or_id
        self.doc_cls = doc_cls or DynamicDocument
        super(CompiledGet, self).__init__(None, params)

    def api_method(self, client):
        return client.get

    def prepare_params(self, params):
        get_params = {}
        if isinstance(self.doc_or_id, Document):
            doc = self.doc_or_id
            for meta_field_name in self.META_FIELDS:
                field_value = getattr(doc, meta_field_name, None)
                param_name = meta_field_name.lstrip('_')
                if field_value is not None:
                    get_params[param_name] = field_value
            self.doc_cls = doc.__class__
        elif isinstance(self.doc_or_id, dict):
            doc = self.doc_or_id
            get_params.update(doc)
            if doc.get('doc_cls'):
                self.doc_cls = doc.pop('doc_cls')
        else:
            doc_id = self.doc_or_id
            get_params.update({'id': doc_id})

        if get_params.get('doc_type') is None:
            get_params['doc_type'] = getattr(
                self.doc_cls, '__doc_type__', None
            )
        get_params.update(params)
        return get_params

    def process_result(self, raw_result):
        return self.doc_cls(_hit=raw_result)


class CompiledMultiGet(CompiledEndpoint):
    compiled_get = None

    class _DocsOrIds(object):
        __visit_name__ = 'docs_or_ids'

        def __init__(self, docs_or_ids):
            self.docs_or_ids = docs_or_ids

        def __iter__(self):
            return iter(self.docs_or_ids)

    def __init__(self, docs_or_ids, params=None, doc_cls=None):
        default_doc_cls = doc_cls
        if isinstance(default_doc_cls, Iterable):
            self.doc_cls_map = {
                _doc_cls.__doc_type__: _doc_cls
                for _doc_cls in default_doc_cls
            }
            self.default_doc_cls = DynamicDocument
        elif default_doc_cls:
            self.doc_cls_map = {}
            self.default_doc_cls = default_doc_cls
        else:
            self.doc_cls_map = {}
            self.default_doc_cls = DynamicDocument

        self.expression = docs_or_ids
        self.doc_classes = []
        super(CompiledMultiGet, self).__init__(
            self._DocsOrIds(docs_or_ids), params
        )

    def api_method(self, client):
        return client.mget

    def visit_docs_or_ids(self, docs_or_ids):
        docs = []
        for doc_or_id in docs_or_ids:
            if isinstance(doc_or_id, Document):
                doc = {
                    '_id': doc_or_id._id,
                }
                if doc_or_id._index:
                    doc['_index'] = doc_or_id._index
                if doc_or_id._version:
                    doc['_version'] = doc_or_id._version
                if doc_or_id._routing:
                    doc['routing'] = doc_or_id._routing
                doc_cls = doc_or_id.__class__
            elif isinstance(doc_or_id, dict):
                doc = doc_or_id
                doc_cls = doc_or_id.pop('doc_cls', None)
            else:
                doc = {'_id': doc_or_id}
                doc_cls = None

            if not doc.get('_type') and hasattr(doc_cls, '__doc_type__'):
                doc['_type'] = doc_cls.__doc_type__

            docs.append(doc)
            self.doc_classes.append(doc_cls)

        return {'docs': docs}

    def process_result(self, raw_result):
        docs = []
        for doc_cls, raw_doc in zip(self.doc_classes, raw_result['docs']):
            doc_type = raw_doc.get('_type')
            if doc_cls is None and doc_type in self.doc_cls_map:
                doc_cls = self.doc_cls_map.get(doc_type)
            if doc_cls is None:
                doc_cls = self.default_doc_cls

            if raw_doc.get('found'):
                docs.append(doc_cls(_hit=raw_doc))
            else:
                docs.append(None)
        return docs


class CompiledDelete(CompiledGet):
    def api_method(self, client):
        return client.delete

    def process_result(self, raw_result):
        return DeleteResult(raw_result)


class CompiledBulk(CompiledEndpoint):
    compiled_meta = None
    compiled_source = None

    class _Actions(object):
        __visit_name__ = 'actions'

        def __init__(self, actions):
            self.actions = actions

        def __iter__(self):
            return iter(self.actions)

    def __init__(self, actions, params=None):
        super(CompiledBulk, self).__init__(self._Actions(actions), params)

    def api_method(self, client):
        return client.bulk

    def visit_actions(self, actions):
        body = []
        for action in actions:
            meta = self.compiled_meta(action).body
            body.append(meta)
            source = self.compiled_source(action).body
            if source is not None:
                body.append(source)
        return body

    def process_result(self, raw_result):
        return BulkResult(raw_result)


class CompiledMeta(Compiled):
    META_FIELD_NAMES = (
        '_id',
        '_index',
        '_type',
        '_routing',
        '_parent',
        '_timestamp',
        '_ttl',
        '_version',
    )

    def __init__(self, doc_or_action):
        super(CompiledMeta, self).__init__(doc_or_action)

    def visit_action(self, action):
        meta = self.visit_document(action.doc)
        meta.update(action.meta_params)
        return {
            action.__action_name__: meta
        }

    def visit_document(self, doc):
        meta = {}
        if isinstance(doc, Document):
            self._populate_meta_from_document(doc, meta)
            doc_type = doc.get_doc_type()
            if doc_type:
                meta['_type'] = doc_type
        else:
            self._populate_meta_from_dict(doc, meta)

        if _is_emulate_doc_types_mode(self.features, doc.__class__):
            meta.pop('_parent', None)
            meta['_id'] = _doc_type_and_id(
                doc.__doc_type__, meta['_id']
            )
            meta['_type'] = DEFAULT_DOC_TYPE

        return meta

    def _populate_meta_from_document(self, doc, meta):
        for field_name in self.META_FIELD_NAMES:
            value = getattr(doc, field_name, None)
            if value:
                meta[field_name] = value

    def _populate_meta_from_dict(self, doc, meta):
        for field_name in self.META_FIELD_NAMES:
            value = doc.get(field_name)
            if value:
                meta[field_name] = value


class CompiledSource(CompiledExpression):
    def __init__(self, doc_or_action, validate=False):
        self._validate = validate
        super(CompiledSource, self).__init__(doc_or_action)

    def visit_action(self, action):
        if action.__action_name__ == 'delete':
            return None

        if isinstance(action.doc, Document):
            doc = self.visit(action.doc)
        else:
            doc = action.doc.copy()
            for exclude_field in Document.mapping_fields:
                doc.pop(exclude_field.get_field().get_name(), None)

        if action.__action_name__ == 'update':
            script = action.source_params.pop('script', None)
            if script:
                source = {'script': self.visit(script)}
            else:
                source = {'doc': doc}
            source.update(self.visit(action.source_params))
        else:
            source = doc

        return source

    def visit_document(self, doc):
        source = {}
        for key, value in doc.__dict__.items():
            if key in doc.__class__.mapping_fields:
                continue

            attr_field = doc.__class__.fields.get(key)
            if not attr_field:
                continue

            if value is None or value == '' or value == []:
                if (
                        self._validate and
                        attr_field.get_field().get_mapping_options().get(
                            'required'
                        )
                ):
                    raise ValidationError("'{}' is required".format(
                        attr_field.get_attr_name()
                    ))
                continue

            value = attr_field.get_type() \
                .from_python(value, self.compiler, validate=self._validate)
            source[attr_field.get_field().get_name()] = value

        for attr_field in doc._fields.values():
            if not self._validate:
                continue

            field = attr_field.get_field()
            if (
                    field.get_mapping_options().get('required')
                    and field.get_name() not in source
            ):
                raise ValidationError(
                    "'{}' is required".format(attr_field.get_attr_name())
                )

        if _is_emulate_doc_types_mode(self.features, doc):
            doc_type_source = {'name': doc.__doc_type__}
            if doc._parent is not None:
                doc_type_source['parent'] = _doc_type_and_id(
                    doc.__parent__.__doc_type__,
                    doc._parent
                )
            source[DOC_TYPE_FIELD_NAME] = doc_type_source

        return source


def _featured_compiler(elasticsearch_features):
    def inject_features(cls):
        class _CompiledExpression(CompiledExpression):
            compiler = cls
            features = elasticsearch_features

        class _CompiledSearchQuery(CompiledSearchQuery):
            compiler = cls
            features = elasticsearch_features

        class _CompiledScroll(CompiledScroll):
            compiler = cls
            features = elasticsearch_features

        class _CompiledCountQuery(CompiledCountQuery):
            compiler = cls
            features = elasticsearch_features

        class _CompiledExistsQuery(CompiledExistsQuery):
            compiler = cls
            features = elasticsearch_features

        class _CompiledDeleteByQuery(CompiledDeleteByQuery):
            compiler = cls
            features = elasticsearch_features

        class _CompiledMultiSearch(CompiledMultiSearch):
            compiler = cls
            features = elasticsearch_features
            compiled_search = _CompiledSearchQuery

        class _CompiledGet(CompiledGet):
            compiler = cls
            features = elasticsearch_features

        class _CompiledMultiGet(CompiledMultiGet):
            compiler = cls
            features = elasticsearch_features

        class _CompiledDelete(CompiledDelete):
            compiler = cls
            features = elasticsearch_features

        class _CompiledMeta(CompiledMeta):
            compiler = cls
            features = elasticsearch_features

        class _CompiledSource(CompiledSource):
            compiler = cls
            features = elasticsearch_features

        class _CompiledBulk(CompiledBulk):
            compiler = cls
            features = elasticsearch_features
            compiled_meta = _CompiledMeta
            compiled_source = _CompiledSource

        class _CompiledPutMapping(CompiledPutMapping):
            compiler = cls
            features = elasticsearch_features

        cls.compiled_expression = _CompiledExpression
        cls.compiled_search_query = _CompiledSearchQuery
        cls.compiled_query = cls.compiled_search_query
        cls.compiled_scroll = _CompiledScroll
        cls.compiled_count_query = _CompiledCountQuery
        cls.compiled_exists_query = _CompiledExistsQuery
        cls.compiled_delete_by_query = _CompiledDeleteByQuery
        cls.compiled_multi_search = _CompiledMultiSearch
        cls.compiled_get = _CompiledGet
        cls.compiled_multi_get = _CompiledMultiGet
        cls.compiled_delete = _CompiledDelete
        cls.compiled_bulk = _CompiledBulk
        cls.compiled_put_mapping = _CompiledPutMapping
        return cls

    return inject_features


@_featured_compiler(
    ElasticsearchFeatures(
        supports_old_boolean_queries=True,
        supports_missing_query=True,
        supports_parent_id_query=False,
        supports_bool_filter=False,
        supports_search_exists_api=True,
        supports_mapping_types=True,
        stored_fields_param='fields',
    )
)
class Compiler_1_0(object):
    pass


@_featured_compiler(
    ElasticsearchFeatures(
        supports_old_boolean_queries=False,
        supports_missing_query=True,
        supports_parent_id_query=False,
        supports_bool_filter=True,
        supports_search_exists_api=True,
        supports_mapping_types=True,
        stored_fields_param='fields',
    )
)
class Compiler_2_0(object):
    pass


@_featured_compiler(
    ElasticsearchFeatures(
        supports_old_boolean_queries=False,
        supports_missing_query=False,
        supports_parent_id_query=True,
        supports_bool_filter=True,
        supports_search_exists_api=False,
        supports_mapping_types=True,
        stored_fields_param='stored_fields',
    )
)
class Compiler_5_0(object):
    pass


@_featured_compiler(
    ElasticsearchFeatures(
        supports_old_boolean_queries=False,
        supports_missing_query=False,
        supports_parent_id_query=True,
        supports_bool_filter=True,
        supports_search_exists_api=False,
        supports_mapping_types=False,
        stored_fields_param='stored_fields',
    )
)
class Compiler_6_0(object):
    pass


Compiler10 = Compiler_1_0

Compiler20 = Compiler_2_0

Compiler50 = Compiler_5_0


def get_compiler_by_es_version(es_version):
    if es_version.major <= 1:
        return Compiler_1_0
    elif es_version.major == 2:
        return Compiler_2_0
    elif es_version.major == 5:
        return Compiler_5_0
    elif es_version.major == 6:
        return Compiler_6_0
    return Compiler_6_0
