import yaml

class QuotedString(str):
    """Clase personalizada para forzar comillas dobles en PyYAML."""
    pass

def quoted_string_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')

yaml.SafeDumper.add_representer(QuotedString, quoted_string_representer)

def _wrap_strings_in_quotes(data):
    """
    Recorre recursivamente un dict/list y convierte los strings a QuotedString.
    También convierte None a strings vacíos entrecomillados para evitar "null" en output.
    """
    if isinstance(data, dict):
        return {k: _wrap_strings_in_quotes(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_wrap_strings_in_quotes(v) for v in data]
    elif isinstance(data, str):
        return QuotedString(data)
    elif data is None:
        # Convertir None a string vacío entrecomillado (no a "null")
        return QuotedString("")
    return data