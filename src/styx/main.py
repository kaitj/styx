import argparse
import json
import pathlib

import tomli as tomllib  # Remove once we move to python 3.11

from styx.compiler.core import compile_boutiques_dict
from styx.compiler.settings import CompilerSettings
from styx.pycodegen.utils import python_snakify


def load_settings_from_toml(
    config_path: pathlib.Path,
    override_input_folder: pathlib.Path | None = None,
    override_output_folder: pathlib.Path | None = None,
) -> CompilerSettings:
    """Load settings from a TOML file."""
    if not config_path.exists():
        if override_input_folder is None:
            raise FileNotFoundError(f"Configuration file {config_path} does not exist")
        return CompilerSettings(
            input_path=override_input_folder,
            output_path=override_output_folder,
        )

    with open(config_path, "rb") as f:
        settings = tomllib.load(f)
        return CompilerSettings(
            input_path=override_input_folder or pathlib.Path(settings.get("input_path", ".")),
            output_path=override_output_folder or pathlib.Path(settings.get("output_path", ".")),
        )


def collect_settings(
    work_dir: pathlib.Path,
    override_input_folder: pathlib.Path | None = None,
    override_output_folder: pathlib.Path | None = None,
    override_config_file: pathlib.Path | None = None,
) -> CompilerSettings:
    """Collect settings."""
    if not work_dir.exists():
        raise FileNotFoundError(f"Work directory {work_dir} does not exist")

    config_file: pathlib.Path | None = None

    if override_config_file is not None:
        config_file = override_config_file
    elif override_input_folder is not None and (override_input_folder / "styx.toml").exists():
        config_file = override_input_folder / "styx.toml"
    elif override_input_folder is not None and (override_input_folder / "pyproject.toml").exists():
        config_file = override_input_folder / "pyproject.toml"
    elif (work_dir / "styx.toml").exists():
        config_file = work_dir / "styx.toml"
    elif (work_dir / "pyproject.toml").exists():
        config_file = work_dir / "pyproject.toml"

    if config_file is not None:
        settings = load_settings_from_toml(
            config_path=config_file,
            override_input_folder=override_input_folder,
            override_output_folder=override_output_folder,
        )
    else:
        settings = CompilerSettings(
            input_path=override_input_folder or work_dir,
            output_path=override_output_folder,
        )

    return settings


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Compile JSON descriptors to Python modules")
    parser.add_argument(
        "-i", "--input-folder", type=pathlib.Path, help="Path to the input folder containing JSON descriptors"
    )
    parser.add_argument(
        "-o", "--output-folder", type=pathlib.Path, help="Path to the output folder for compiled Python modules"
    )
    parser.add_argument("-c", "--config", type=pathlib.Path, help="Path to the configuration file")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    settings = collect_settings(
        work_dir=pathlib.Path.cwd(),
        override_input_folder=args.input_folder,
        override_output_folder=args.output_folder,
        override_config_file=args.config,
    )
    settings.debug_mode = settings.debug_mode or args.debug

    assert settings.input_path is not None
    json_files = settings.input_path.glob("**/*.json")

    fail_counter = 0
    total_counter = 0
    for json_path in json_files:
        total_counter += 1
        output_module_path = json_path.parent.relative_to(settings.input_path).parts
        # ensure module path is valid python symbol
        output_module_path = tuple(python_snakify(part) for part in output_module_path)
        output_file_name = f"{python_snakify(json_path.stem)}.py"

        with open(json_path, "r", encoding="utf-8") as json_file:
            try:
                json_data = json.load(json_file)
            except json.JSONDecodeError:
                print(f"Skipped: {json_path} (invalid JSON)")
                fail_counter += 1
                continue
        try:
            code = compile_boutiques_dict(json_data, settings)

            if settings.output_path:
                output_path = settings.output_path / pathlib.Path(*output_module_path)
                output_path.mkdir(parents=True, exist_ok=True)
                output_path = output_path / output_file_name
                with open(output_path, "w") as py_file:
                    py_file.write(code)
                print(f"Compiled {json_path} to {output_path}")
            else:
                print(f"Compiled {json_path} -> {pathlib.Path(*output_module_path) / output_file_name}: {'---' * 10}")
                print(code)
                print("---" * 10)
        except Exception as e:
            print(f"Skipped: {json_path}")
            if settings.debug_mode:
                raise e
            fail_counter += 1
            import traceback

            print(traceback.format_exc())

    if fail_counter > 0:
        print(f"Failed to compile {fail_counter}/{total_counter} descriptors.")
    else:
        print(f"Successfully compiled {total_counter} descriptors.")


if __name__ == "__main__":
    main()
