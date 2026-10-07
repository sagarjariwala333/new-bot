"""
Only used when pydantic isn't actually installed (this project's own dev
sandbox lacks network access to pip install it). In a real deployment the
real pydantic is present and this stub is never registered.

IMPORTANT HONESTY NOTE: this stub makes BaseModel subclasses constructible
and stores field values as plain attributes, using each field's default
(or raising if a required field is missing) - enough to IMPORT
app/api/schemas.py and app/api/__init__.py and catch real Python-level bugs
(undefined names, broken decorators, wrong signatures) that ast.parse
cannot. It deliberately does NOT implement real pydantic validation
(min_length, ge/le, regex, etc. are accepted as arguments but never
enforced) - a stub that pretended to validate would risk giving false
confidence that constraint logic is correct when it was never actually
exercised. Constraint correctness still needs a real pydantic environment
to verify; this stub only proves the code is structurally sound.
"""
import sys
import types

try:
    import pydantic  # noqa: F401
except ImportError:
    stub = types.ModuleType("pydantic")

    _MISSING = object()

    class FieldInfo:
        def __init__(self, default):
            self.default = default

    def Field(default=_MISSING, **kwargs):
        # `default` here may itself be `...` (Ellipsis) for a required
        # field, or a real default value - both are preserved as-is via
        # FieldInfo; constraint kwargs (ge, le, min_length, etc.) are
        # accepted but intentionally not enforced (see module docstring).
        if default is _MISSING:
            default = kwargs.get("default", ...)
        return FieldInfo(default)

    class BaseModel:
        def __init__(self, **data):
            annotations = {}
            for klass in reversed(type(self).__mro__):
                annotations.update(getattr(klass, "__annotations__", {}))
            for name in annotations:
                if name in data:
                    setattr(self, name, data[name])
                    continue
                class_default = getattr(type(self), name, _MISSING)
                if isinstance(class_default, FieldInfo):
                    if class_default.default is ...:
                        raise ValueError(f"{name} is required")
                    setattr(self, name, class_default.default)
                elif class_default is not _MISSING:
                    setattr(self, name, class_default)
                else:
                    raise ValueError(f"{name} is required")

        def model_dump(self, exclude_unset=False):
            return dict(self.__dict__)

    def field_validator(*fields, **kwargs):
        def decorator(func):
            return classmethod(func) if not isinstance(func, classmethod) else func
        return decorator

    def model_validator(*a, **kwargs):
        def decorator(func):
            return classmethod(func) if not isinstance(func, classmethod) else func
        return decorator

    stub.BaseModel = BaseModel
    stub.Field = Field
    stub.field_validator = field_validator
    stub.model_validator = model_validator
    sys.modules["pydantic"] = stub
