def parse_cmp_xy_tokens(tokens, designator_idx):
    """Return the physical X/Y coordinates from one CMP ``X`` record."""
    if len(tokens) > 6 and tokens[0] == "X":
        try:
            return float(tokens[5]), float(tokens[6])
        except (TypeError, ValueError):
            pass

    # Bounded fallback for compatible CMP variants: X/Y are four and three
    # fields before the designator (Z and part number follow them).
    try:
        return float(tokens[designator_idx - 4]), float(tokens[designator_idx - 3])
    except (IndexError, TypeError, ValueError):
        return None, None


def parse_cmp_pin_id(tokens):
    """Return the stable numeric pin id from one CMP ``X`` record."""
    try:
        if tokens[0] == "X":
            return str(int(tokens[1]))
    except (IndexError, TypeError, ValueError):
        pass
    return ""
