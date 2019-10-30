

def md_table(headers, values) -> str:
    """
    Turn headers and values into a valid markdown table.
    """
    out = ''
    col_lengths = [0] * len(headers)

    for row in tuple([headers]) + tuple(values):
        for i, val in enumerate(row):
            col_lengths[i] = max(len(str(val)), col_lengths[i])

    row_fmt = '|' + ''.join(' {' + str(i) + ':<' + str(collen + 2) + '} |'
                            for i, collen in enumerate(col_lengths)) + '\n'

    dividers = ['-' * col_lengths[i] for i, h in enumerate(headers)]
    out += row_fmt.format(*headers)
    out += row_fmt.format(*dividers)

    for row in values:
        out += row_fmt.format(*[str(i) for i in row])

    return out
