import styx.ir.core as ir
from styx.backend.generic.documentation import docs_to_docstring
from styx.backend.generic.gen.constraints import struct_compile_constraint_checks
from styx.backend.generic.gen.lookup import LookupParam
from styx.backend.generic.gen.metadata import generate_static_metadata
from styx.backend.generic.languageprovider import LanguageProvider, MStr
from styx.backend.generic.linebuffer import LineBuffer
from styx.backend.generic.model import GenericArg, GenericDataClass, GenericFunc, GenericModule, GenericNamedTuple
from styx.backend.generic.scope import Scope
from styx.backend.generic.utils import enquote, struct_has_outputs


def _compile_struct(
    lang: LanguageProvider,
    struct: ir.Param[ir.Param.Struct],
    interface_module: GenericModule,
    lookup: LookupParam,
    metadata_symbol: str,
    root_function: bool,
    stdout_as_string_output: ir.StdOutErrAsStringOutput | None = None,
    stderr_as_string_output: ir.StdOutErrAsStringOutput | None = None,
) -> None:
    has_outputs = root_function or struct_has_outputs(struct)

    outputs_type = lookup.py_output_type[struct.base.id_]

    func_cargs_building: GenericFunc
    if root_function:
        func_cargs_building = GenericFunc(
            name=lookup.py_type[struct.base.id_],
            return_type=outputs_type,
            return_descr=f"NamedTuple of outputs (described in `{outputs_type}`).",
            docstring_body=docs_to_docstring(struct.base.docs),
        )
        pyargs = func_cargs_building.args
    else:
        func_cargs_building = GenericFunc(
            name="run",
            docstring_body="Build command line arguments. This method is called by the main command.",
            return_type=lang.type_string_list(),
            return_descr="Command line arguments",
            args=[
                GenericArg(
                    name=lang.expr_self(),
                    type=None,
                    default=None,
                    docstring="The sub-command object.",
                ),
                GenericArg(
                    name=lang.symbol_execution(),
                    type=lang.type_execution(),
                    default=None,
                    docstring="The execution object.",
                ),
            ],
        )
        struct_class: GenericDataClass = GenericDataClass(
            name=lookup.py_struct_type[struct.base.id_],
            docstring=docs_to_docstring(struct.base.docs),
            methods=[func_cargs_building],
        )
        if has_outputs:
            func_outputs = GenericFunc(
                name="outputs",
                docstring_body="Collect output file paths.",
                return_type=outputs_type,
                return_descr=f"NamedTuple of outputs (described in `{outputs_type}`).",
                args=[
                    GenericArg(name=lang.expr_self(), type=None, default=None, docstring="The sub-command object."),
                    GenericArg(
                        name=lang.symbol_execution(),
                        type=lang.type_execution(),
                        default=None,
                        docstring="The execution object.",
                    ),
                ],
            )
        pyargs = struct_class.fields

    # Collect param python symbols
    for elem in struct.body.iter_params():
        symbol = lookup.py_symbol[elem.base.id_]
        pyargs.append(
            GenericArg(
                name=symbol,
                type=lookup.py_type[elem.base.id_],
                default=lang.param_default_value(elem),
                docstring=elem.base.docs.description,
            )
        )

        if isinstance(elem.body, ir.Param.Struct):
            _compile_struct(
                lang=lang,
                struct=elem,
                interface_module=interface_module,
                lookup=lookup,
                metadata_symbol=metadata_symbol,
                root_function=False,
            )
        elif isinstance(elem.body, ir.Param.StructUnion):
            for child in elem.body.alts:
                _compile_struct(
                    lang=lang,
                    struct=child,
                    interface_module=interface_module,
                    lookup=lookup,
                    metadata_symbol=metadata_symbol,
                    root_function=False,
                )

    struct_compile_constraint_checks(lang=lang, func=func_cargs_building, struct=struct, lookup=lookup)

    if has_outputs:
        _compile_outputs_class(
            lang=lang,
            struct=struct,
            interface_module=interface_module,
            lookup=lookup,
            stdout_as_string_output=stdout_as_string_output,
            stderr_as_string_output=stderr_as_string_output,
        )

    if root_function:
        func_cargs_building.body.extend([
            *lang.runner_declare("runner"),
            *lang.execution_declare("execution", metadata_symbol),
        ])

    _compile_cargs_building(lang, struct, lookup, func_cargs_building, access_via_self=not root_function)

    if root_function:
        pyargs.append(
            GenericArg(
                name="runner",
                type=lang.type_optional(lang.type_runner()),
                default=lang.expr_null(),
                docstring="Command runner",
            )
        )
        _compile_outputs_building(
            lang=lang,
            struct=struct,
            func=func_cargs_building,
            lookup=lookup,
            access_via_self=False,
            stderr_as_string_output=stderr_as_string_output,
            stdout_as_string_output=stdout_as_string_output,
        )
        func_cargs_building.body.extend([
            *lang.execution_run(
                execution_symbol="execution",
                cargs_symbol="cargs",
                stdout_output_symbol=lookup.py_output_field_symbol[stdout_as_string_output.id_]
                if stdout_as_string_output
                else None,
                stderr_output_symbol=lookup.py_output_field_symbol[stderr_as_string_output.id_]
                if stderr_as_string_output
                else None,
            ),
            lang.return_statement("ret"),
        ])
        interface_module.funcs_and_classes.append(func_cargs_building)
    else:
        if has_outputs:
            _compile_outputs_building(
                lang=lang,
                struct=struct,
                func=func_outputs,
                lookup=lookup,
                access_via_self=True,
            )
            func_outputs.body.extend([lang.return_statement("ret")])
            struct_class.methods.append(func_outputs)
        func_cargs_building.body.extend([lang.return_statement("cargs")])
        interface_module.funcs_and_classes.append(struct_class)
        interface_module.exports.append(struct_class.name)


def _compile_cargs_building(
    lang: LanguageProvider,
    param: ir.Param[ir.Param.Struct],
    lookup: LookupParam,
    func: GenericFunc,
    access_via_self: bool,
) -> None:
    func.body.extend(lang.cargs_declare("cargs"))

    for group in param.body.groups:
        group_conditions_py = []

        # We're collecting two structurally equal to versions of cargs string expressions,
        # one that assumes all parameters are set and one that checks all of them.
        # This way later we can use one or the other depending on the surrounding context.
        cargs_exprs: list[MStr] = []  # string expressions for building cargs
        cargs_exprs_maybe_null: list[MStr] = []  # string expressions for building cargs if parameters may be null

        for carg in group.cargs:
            carg_exprs: list[MStr] = []  # string expressions for building a single carg
            carg_exprs_maybe_null: list[MStr] = []

            # Build single carg
            for token in carg.tokens:
                if isinstance(token, str):
                    carg_exprs.append(MStr(lang.expr_literal(token), False))
                    carg_exprs_maybe_null.append(MStr(lang.expr_literal(token), False))
                    continue
                elem_symbol = lookup.py_symbol[token.base.id_]
                if access_via_self:
                    elem_symbol = lang.expr_access_attr_via_self(elem_symbol)
                param_as_mstr = lang.param_var_to_mstr(token, elem_symbol)
                carg_exprs.append(param_as_mstr)
                if (param_is_set_expr := lang.param_var_is_set_by_user(token, elem_symbol, False)) is not None:
                    group_conditions_py.append(param_is_set_expr)
                    _empty_expr = lang.mstr_empty_literal_like(param_as_mstr)
                    carg_exprs_maybe_null.append(
                        MStr(
                            lang.expr_ternary(param_is_set_expr, param_as_mstr.expr, _empty_expr, True),
                            param_as_mstr.is_list,
                        )
                    )
                else:
                    carg_exprs_maybe_null.append(param_as_mstr)

            # collapse and add single carg to cargs expressions
            if len(carg_exprs) == 1:
                cargs_exprs.append(carg_exprs[0])
                cargs_exprs_maybe_null.append(carg_exprs_maybe_null[0])
            else:
                cargs_exprs.append(lang.mstr_concat(carg_exprs))
                cargs_exprs_maybe_null.append(lang.mstr_concat(carg_exprs_maybe_null))

        # Append to cargs buffer
        buf_appending: LineBuffer = []
        if len(cargs_exprs) == 1:
            for str_symbol in cargs_exprs_maybe_null if len(group_conditions_py) > 1 else cargs_exprs:
                buf_appending.extend(lang.mstr_cargs_add("cargs", str_symbol))
        else:
            x = cargs_exprs_maybe_null if len(group_conditions_py) > 1 else cargs_exprs
            buf_appending.extend(lang.mstr_cargs_add("cargs", x))

        if len(group_conditions_py) > 0:
            func.body.extend(
                lang.if_else_block(
                    condition=lang.expr_conditions_join_or(group_conditions_py),
                    truthy=buf_appending,
                )
            )
        else:
            func.body.extend(buf_appending)


def _compile_outputs_class(
    lang: LanguageProvider,
    struct: ir.Param[ir.Param.Struct],
    interface_module: GenericModule,
    lookup: LookupParam,
    stdout_as_string_output: ir.StdOutErrAsStringOutput | None = None,
    stderr_as_string_output: ir.StdOutErrAsStringOutput | None = None,
) -> None:
    outputs_class: GenericNamedTuple = GenericNamedTuple(
        name=lookup.py_output_type[struct.base.id_],
        docstring=f"Output object returned when calling `{lookup.py_type[struct.base.id_]}(...)`.",
    )
    outputs_class.fields.append(
        GenericArg(
            name="root",
            type="OutputPathType",
            default=None,
            docstring="Output root folder. This is the root folder for all outputs.",
        )
    )

    for stdout_stderr_output in (stdout_as_string_output, stderr_as_string_output):
        if stdout_stderr_output is None:
            continue
        outputs_class.fields.append(
            GenericArg(
                name=lookup.py_output_field_symbol[stdout_stderr_output.id_],
                type=lang.type_string_list(),
                default=None,
                docstring=stdout_stderr_output.docs.description,
            )
        )

    for output in struct.base.outputs:
        output_symbol = lookup.py_output_field_symbol[output.id_]

        # Optional if any of its param references is optional
        optional = False
        for token in output.tokens:
            if isinstance(token, str):
                continue
            optional = optional or lookup.param[token.ref_id].nullable

        output_type = lang.type_output_path()
        if optional:
            output_type = lang.type_optional(output_type)

        outputs_class.fields.append(
            GenericArg(
                name=output_symbol,
                type=output_type,
                default=None,
                docstring=output.docs.description,
            )
        )

    for sub_struct in struct.body.iter_params():
        if isinstance(sub_struct.body, ir.Param.Struct):
            if struct_has_outputs(sub_struct):
                output_type = lookup.py_output_type[sub_struct.base.id_]
                if sub_struct.list_:
                    output_type = lang.type_list(output_type)
                if sub_struct.nullable:
                    output_type = lang.type_optional(output_type)

                output_symbol = lookup.py_output_field_symbol[sub_struct.base.id_]

                input_type = lookup.py_struct_type[sub_struct.base.id_]
                docs_append = ""
                if sub_struct.list_:
                    docs_append = "This is a list of outputs with the same length and order as the inputs."

                outputs_class.fields.append(
                    GenericArg(
                        name=output_symbol,
                        type=output_type,
                        default=None,
                        docstring=f"Outputs from {enquote(input_type, '`')}.{docs_append}",
                    )
                )
        elif isinstance(sub_struct.body, ir.Param.StructUnion):
            if any([struct_has_outputs(s) for s in sub_struct.body.alts]):
                alt_types = [
                    lookup.py_output_type[sub_command.base.id_]
                    for sub_command in sub_struct.body.alts
                    if struct_has_outputs(sub_command)
                ]
                if len(alt_types) > 0:
                    output_type = lang.type_union(alt_types)

                    if sub_struct.list_:
                        output_type = lang.type_list(output_type)
                    if sub_struct.nullable:
                        output_type = lang.type_optional(output_type)

                    output_symbol = lookup.py_output_field_symbol[sub_struct.base.id_]

                    alt_input_types = [
                        lookup.py_struct_type[sub_command.base.id_]
                        for sub_command in sub_struct.body.alts
                        if struct_has_outputs(sub_command)
                    ]
                    docs_append = ""
                    if sub_struct.list_:
                        docs_append = "This is a list of outputs with the same length and order as the inputs."

                    input_types_human = " or ".join([enquote(t, "`") for t in alt_input_types])
                    outputs_class.fields.append(
                        GenericArg(
                            name=output_symbol,
                            type=output_type,
                            default=None,
                            docstring=f"Outputs from {input_types_human}.{docs_append}",
                        )
                    )

    interface_module.funcs_and_classes.append(outputs_class)
    interface_module.exports.append(outputs_class.name)


def _compile_outputs_building(
    lang: LanguageProvider,
    struct: ir.Param[ir.Param.Struct],
    func: GenericFunc,
    lookup: LookupParam,
    access_via_self: bool = False,
    stdout_as_string_output: ir.StdOutErrAsStringOutput | None = None,
    stderr_as_string_output: ir.StdOutErrAsStringOutput | None = None,
) -> None:
    """Generate the outputs building code."""
    members = {}

    def _py_get_val(
        output_param_reference: ir.OutputParamReference,
    ) -> str:
        param = lookup.param[output_param_reference.ref_id]
        symbol = lookup.py_symbol[param.base.id_]

        substitute = symbol
        if access_via_self:
            substitute = lang.expr_access_attr_via_self(substitute)

        if param.list_:
            raise Exception(f"Output path template replacements cannot be lists. ({param.base.name})")

        if isinstance(param.body, ir.Param.String):
            return lang.expr_remove_suffixes(substitute, output_param_reference.file_remove_suffixes)

        if isinstance(param.body, (ir.Param.Int, ir.Param.Float)):
            return lang.expr_numeric_to_str(substitute)

        if isinstance(param.body, ir.Param.File):
            return lang.expr_remove_suffixes(
                lang.expr_path_get_filename(substitute), output_param_reference.file_remove_suffixes
            )

        if isinstance(param.body, ir.Param.Bool):
            raise Exception(f"Unsupported input type for output path template of '{param.base.name}'.")
        assert False

    for stdout_stderr_output in (stdout_as_string_output, stderr_as_string_output):
        if stdout_stderr_output is None:
            continue
        output_symbol = lookup.py_output_field_symbol[stdout_stderr_output.id_]

        members[output_symbol] = lang.expr_empty_str_list()

    for output in struct.base.outputs:
        output_symbol = lookup.py_output_field_symbol[output.id_]

        output_segments: list[str] = []
        conditions = []
        for token in output.tokens:
            if isinstance(token, str):
                output_segments.append(lang.expr_literal(token))
                continue
            output_segments.append(_py_get_val(token))

            ostruct = lookup.param[token.ref_id]
            param_symbol = lookup.py_symbol[ostruct.base.id_]
            if (py_var_is_set_by_user := lang.param_var_is_set_by_user(ostruct, param_symbol, False)) is not None:
                conditions.append(py_var_is_set_by_user)

        if len(conditions) > 0:
            members[output_symbol] = lang.expr_ternary(
                condition=lang.expr_conditions_join_and(conditions),
                truthy=lang.resolve_output_file("execution", lang.expr_concat_strs(output_segments)),
                falsy=lang.expr_null(),
            )
        else:
            members[output_symbol] = lang.resolve_output_file("execution", lang.expr_concat_strs(output_segments))

    # sub struct outputs
    for sub_struct in struct.body.iter_params():
        has_outputs = False
        if isinstance(sub_struct.body, ir.Param.Struct):
            has_outputs = struct_has_outputs(sub_struct)
        elif isinstance(sub_struct.body, ir.Param.StructUnion):
            has_outputs = any([struct_has_outputs(s) for s in sub_struct.body.alts])
        if not has_outputs:
            continue

        output_symbol = lookup.py_output_field_symbol[sub_struct.base.id_]
        output_symbol_resolved = lookup.py_symbol[sub_struct.base.id_]
        if access_via_self:
            output_symbol_resolved = lang.expr_access_attr_via_self(output_symbol_resolved)

        members[output_symbol] = lang.struct_collect_outputs(sub_struct, output_symbol_resolved)

    lang.generate_ret_object_creation(
        buf=func.body,
        execution_symbol="execution",
        output_type=lookup.py_output_type[struct.base.id_],
        members=members,
    )


def compile_interface(
    lang: LanguageProvider,
    interface: ir.Interface,
    package_scope: Scope,
    interface_module: GenericModule,
) -> None:
    """Entry point to the Python backend."""
    interface_module.imports.extend(lang.wrapper_module_imports())

    metadata_symbol = generate_static_metadata(
        lang=lang,
        module=interface_module,
        scope=package_scope,
        interface=interface,
    )
    interface_module.exports.append(metadata_symbol)

    function_symbol = package_scope.add_or_dodge(lang.symbol_var_case_from(interface.command.base.name))
    interface_module.exports.append(function_symbol)

    function_scope = Scope(lang).language_base_scope()
    function_scope.add_or_die("runner")
    function_scope.add_or_die("execution")
    function_scope.add_or_die("cargs")
    function_scope.add_or_die("ret")

    # Lookup tables
    lookup = LookupParam(
        lang=lang,
        interface=interface,
        package_scope=package_scope,
        function_symbol=function_symbol,
        function_scope=function_scope,
    )

    _compile_struct(
        lang=lang,
        struct=interface.command,
        interface_module=interface_module,
        lookup=lookup,
        metadata_symbol=metadata_symbol,
        root_function=True,
        stdout_as_string_output=interface.stdout_as_string_output,
        stderr_as_string_output=interface.stderr_as_string_output,
    )
