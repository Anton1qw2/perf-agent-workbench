"""MCP JSON Schema validation without remote reference retrieval."""

def validate_schema(value, schema):
    """Validate server schemas without resolving references over the network."""
    from jsonschema.validators import validator_for
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def reject(uri):
        raise NoSuchResource(ref=uri)

    validator = validator_for(schema)
    validator.check_schema(schema)
    validator(schema, registry=Registry(retrieve=reject)).validate(value)
