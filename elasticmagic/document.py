import fnmatch

from .types import String, Integer, Float, Date
from .compiler import MappingCompiled
from .attribute import AttributedField, DynamicAttributedField, _attributed_field_factory
from .expression import Field, MappingField
from .datastructures import OrderedAttributes
from .util import cached_property
from .compat import with_metaclass


MAPPING_FIELD_NAMES = {
    '_uid',
    '_id',
    '_type',
    '_version',
    '_source',
    '_all',
    '_analyzer',
    '_parent',
    '_routing',
    '_index',
    '_size',
    '_timestamp',
    '_ttl',
    '_score',
}

META_FIELD_NAMES = {
    '_id',
    '_index',
    '_type',
    '_version',
    '_routing',
    '_parent',
    '_timestamp',
    '_ttl',
}


class DocumentMeta(type):
    def __new__(meta, name, bases, dct):
        cls = type.__new__(meta, name, bases, dct)

        cls._dynamic_defaults = cls._get_dynamic_defaults()

        cls._fields = OrderedAttributes(defaults=cls._dynamic_defaults)
        cls._user_fields = OrderedAttributes(defaults=cls._dynamic_defaults)
        cls._mapping_fields = OrderedAttributes()
        cls._dynamic_fields = OrderedAttributes()
        cls._field_name_map = {}

        process_fields = []

        for attr_name in dir(cls):
            field = getattr(cls, attr_name)
            if isinstance(field, AttributedField):
                if field._attr not in cls.__dict__:
                    # inherited from base document class
                    process_fields.append((attr_name, field._field))
            elif isinstance(field, Field):
                process_fields.append((attr_name, field))
        process_fields = sorted(process_fields, key=lambda v: v[1]._count)

        for attr_name, field in process_fields:
            if attr_name in cls.__dict__:
                delattr(cls, attr_name)
            setattr(cls, attr_name, field)

        for dyn_field in cls.__dynamic_fields__:
            cls._dynamic_fields[dyn_field.get_name()] = AttributedField(cls, dyn_field.get_name(), dyn_field)

        return cls

    def _get_dynamic_defaults(cls):
        dynamic_defaults = {}
        for dyn_field in cls.__dynamic_fields__:
            default = _attributed_field_factory(AttributedField, cls, dyn_field)
            dynamic_defaults[dyn_field.get_name()] = default
        return dynamic_defaults

    def __setattr__(cls, name, value):
        if isinstance(value, Field):
            if name in MAPPING_FIELD_NAMES:
                field = value.clone(cls=MappingField)
            else:
                field = value.clone()

            if field._name is None:
                field._name = name

            attr_field = AttributedField(cls, name, field)

            if name in MAPPING_FIELD_NAMES:
                cls._mapping_fields[name] = attr_field
            else:
                cls._user_fields[name] = attr_field
            cls._fields[name] = attr_field
            cls._field_name_map[field._name] = attr_field

            value = attr_field

        super(DocumentMeta, cls).__setattr__(name, value)
    
    @property
    def fields(cls):
        return cls._fields

    @property
    def user_fields(cls):
        return cls._user_fields

    @property
    def mapping_fields(cls):
        return cls._mapping_fields

    @property
    def dynamic_fields(cls):
        return cls._dynamic_fields

    def wildcard(cls, name):
        return DynamicAttributedField(cls, name, Field(name))

    def __getattr__(cls, name):
        return getattr(cls.fields, name)


class Document(with_metaclass(DocumentMeta)):
    __visit_name__ = 'document'

    _uid = Field(String)
    _id = Field(String)
    _type = Field(String)
    _version = Field(Integer)
    _source = Field(String)
    _all = Field(String)
    _analyzer = Field(String)
    _parent = Field(String)
    _routing = Field(String)
    _index = Field(String)
    _size = Field(Integer)
    _timestamp = Field(Date)
    _ttl = Field(String)
    _score = Field(Float)

    __dynamic_fields__ = []

    __mapping_options__ = {}

    def __init__(self, _hit=None, _result=None, **kwargs):
        self._index = self._type = self._id = self._score = None
        if _hit:
            self._score = _hit.get('_score')
            for attr_field in self._mapping_fields:
                setattr(self, attr_field._attr, _hit.get(attr_field._field._name))
            if _hit.get('_source'):
                for hit_key, hit_value in _hit['_source'].items():
                    setattr(self, *self._process_hit_key_value(hit_key, hit_value))

        for fkey, fvalue in kwargs.items():
            setattr(self, fkey, fvalue)

        self._result = _result

    def _process_hit_key_value(self, key, value):
        if key in self._field_name_map:
            attr_field = self._field_name_map[key]
            return attr_field._attr, attr_field._to_python(value)
        return key, value

    def to_meta(self):
        doc_meta = {}
        if hasattr(self, '__doc_type__'):
            doc_meta['_type'] = self.__doc_type__
        for field_name in META_FIELD_NAMES:
            value = getattr(self, field_name, None)
            if value:
                doc_meta[field_name] = value
        return doc_meta
    
    def to_source(self):
        res = {}
        for key, value in self.__dict__.items():
            if key in self.__class__.mapping_fields:
                continue
            if value is None or value == '' or value == []:
                continue

            attr_field = self.__class__.fields.get(key)
            if attr_field:
                res[attr_field._attr] = attr_field._from_python(value)

        return res

    @classmethod
    def to_mapping(cls):
        return MappingCompiled(cls).params

    @cached_property
    def instance(self):
        if self._result:
            self._result._populate_instances(self.__class__)
            return self.__dict__['instance']


class DynamicDocumentMeta(DocumentMeta):
    def _get_dynamic_defaults(cls):
        dynamic_defaults = super(DynamicDocumentMeta, cls)._get_dynamic_defaults()
        if '*' not in dynamic_defaults:
            dynamic_defaults['*'] = _attributed_field_factory(DynamicAttributedField, cls, Field('*'))
        return dynamic_defaults

    def __getattr__(cls, name):
        return cls.fields[name]


class DynamicDocument(with_metaclass(DynamicDocumentMeta, Document)):
    def _process_hit_key_value(self, key, value):
        key, value = super(DynamicDocument, self)._process_hit_key_value(key, value)
        if isinstance(value, dict):
            return key, DynamicDocument(**value)
        return key, value

