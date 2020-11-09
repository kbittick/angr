from typing import Tuple, Optional

from ..sim_type import parse_file, parse_cpp_file, normalize_cpp_function_name, SimTypeCppFunction, SimTypeFd


def get_function_name(s):
    """
    Get the function name from a C-style function declaration string.

    :param str s: A C-style function declaration string.
    :return:      The function name.
    :rtype:       str
    """

    s = s.strip()
    if s.startswith("__attribute__"):
        # Remove "__attribute__ ((foobar))"
        if "))" not in s:
            raise ValueError("__attribute__ is present, but I cannot find double-right parenthesis in the function "
                             "declaration string.")

        s = s[s.index("))") + 2 : ].strip()

    if '(' not in s:
        raise ValueError("Cannot find any left parenthesis in the function declaration string.")

    func_name = s[:s.index('(')].strip()

    for i, ch in enumerate(reversed(func_name)):
        if ch == ' ':
            pos = len(func_name) - 1 - i
            break
    else:
        raise ValueError('Cannot find any space in the function declaration string.')

    func_name = func_name[pos + 1 : ]
    return func_name


def convert_cproto_to_py(c_decl):
    """
    Convert a C-style function declaration string to its corresponding SimTypes-based Python representation.

    :param str c_decl:              The C-style function declaration string.
    :return:                        A tuple of the function name, the prototype, and a string representing the
                                    SimType-based Python representation.
    :rtype:                         tuple
    """

    s = [ ]

    try:
        s.append('# %s' % c_decl)  # comment string

        parsed = parse_file(c_decl)
        parsed_decl = parsed[0]
        if not parsed_decl:
            raise ValueError('Cannot parse the function prototype.')

        func_name, func_proto = next(iter(parsed_decl.items()))

        s.append('"%s": %s,' % (func_name, func_proto._init_str()))  # The real Python string

    except Exception:  # pylint:disable=broad-except
        # Silently catch all parsing errors... supporting all function declarations is impossible
        try:
            func_name = get_function_name(c_decl)
            func_proto = None
            s.append('"%s": None,' % func_name)
        except ValueError:
            # Failed to extract the function name. Is it a function declaration?
            func_name, func_proto = None, None

    return func_name, func_proto, "\n".join(s)


def convert_cppproto_to_py(cpp_decl: str,
                           with_param_names: bool=False) -> Tuple[Optional[str],Optional[SimTypeCppFunction],Optional[str]]:
    """
    Pre-process a C++-style function declaration string to its corresponding SimTypes-based Python representation.

    :param cpp_decl:    The C++-style function declaration string.
    :return:            A tuple of the function name, the prototype, and a string representing the SimType-based Python
                        representation.
    """

    s = [ ]
    try:
        s.append("# %s" % cpp_decl)

        parsed = parse_cpp_file(cpp_decl, with_param_names=with_param_names)
        parsed_decl = parsed[0]
        if not parsed_decl:
            raise ValueError("Cannot parse the function prototype.")

        func_name, func_proto = next(iter(parsed_decl.items()))

        s.append('"%s": %s,' % (func_name, func_proto._init_str()))  # The real Python string

    except Exception:  # pylint:disable=broad-except
        try:
            func_name = get_function_name(cpp_decl)
            func_proto = None
            s.append('"%s": None,' % func_name)
        except ValueError:
            # Failed to extract the function name. Is it a function declaration?
            func_name, func_proto = None, None

    return func_name, func_proto, "\n".join(s)


def cprotos2py(cprotos, fd_spots=frozenset(), remove_sys_prefix=False):
    """
    Parse a list of C function declarations and output to Python code that can be embedded into
    angr.procedures.definitions.

    >>> # parse the list of glibc C prototypes and output to a file
    >>> from angr.procedures.definitions import glibc
    >>> with open("glibc_protos", "w") as f: f.write(cprotos2py(glibc._libc_c_decls))

    :param list cprotos:    A list of C prototype strings.
    :return:                A Python string.
    :rtype:                 str
    """
    s = ""
    for decl in cprotos:
        func_name, proto_, str_ = convert_cproto_to_py(decl)  # pylint:disable=unused-variable
        if remove_sys_prefix and func_name.startswith('sys'):
            func_name = '_'.join(func_name.split('_')[1:])
        if proto_ is not None:
            if (func_name, -1) in fd_spots:
                proto_.returnty = SimTypeFd(label=proto_.returnty.label)
            for i, arg in enumerate(proto_.args):
                if (func_name, i) in fd_spots:
                    proto_.args[i] = SimTypeFd(label=arg.label)

        line1 = ' '*8 + '# ' + decl + '\n'
        line2 = ' '*8 + repr(func_name) + ": " + (proto_._init_str() if proto_ is not None else "None") + ',' + '\n'
        s += line1 + line2
    return s


def get_cpp_function_name(demangled_name, specialized=True, qualified=True):
    if not specialized:
        # remove "<???>"s
        name = normalize_cpp_function_name(demangled_name)
    else:
        name = demangled_name

    if not qualified:
        # remove leading namespaces
        chunks = name.split("::")
        name = "::".join(chunks[-2:])

    # remove arguments
    if "(" in name:
        name = name[: name.find("(")]

    return name
