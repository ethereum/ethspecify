import difflib
import functools
import glob
import hashlib
import io
import os
import re
import requests
import textwrap
import tokenize
import yaml


def validate_exception_items(exceptions, version, require_exceptions_have_fork=False):
    """
    Validate that exception items actually exist in the spec.
    Raises an exception if any item doesn't exist.
    """
    if not exceptions:
        return

    # Get the pyspec data
    try:
        pyspec = get_pyspec(version)
    except Exception as e:
        print(f"Warning: Could not validate exceptions - failed to load pyspec: {e}")
        return

    # Map exception keys to pyspec keys
    exception_to_pyspec_map = {
        'functions': 'functions',
        'fn': 'functions',
        'constants': 'constant_vars',
        'constant_variables': 'constant_vars',
        'constant_var': 'constant_vars',
        'configs': 'config_vars',
        'config_variables': 'config_vars',
        'config_var': 'config_vars',
        'presets': 'preset_vars',
        'preset_variables': 'preset_vars',
        'preset_var': 'preset_vars',
        'ssz_objects': 'ssz_objects',
        'containers': 'ssz_objects',
        'container': 'ssz_objects',
        'dataclasses': 'dataclasses',
        'dataclass': 'dataclasses',
        'custom_types': 'custom_types',
        'custom_type': 'custom_types'
    }

    errors = []

    for exception_key, exception_items in exceptions.items():
        # Get the corresponding pyspec key
        pyspec_key = exception_to_pyspec_map.get(exception_key)
        if not pyspec_key:
            errors.append(f"Unknown exception type: '{exception_key}'")
            continue

        # Ensure exception_items is a list
        if not isinstance(exception_items, list):
            exception_items = [exception_items]

        for item in exception_items:
            # Parse item#fork format
            if '#' in item:
                item_name, fork = item.split('#', 1)
            else:
                # If no fork specified, we'll check if it exists in any fork
                item_name = item
                fork = None
                if require_exceptions_have_fork:
                    errors.append(f"invalid key: {exception_key}.{item_name} (missing fork)")
                    continue

            # Check if the item exists
            item_found = False
            if fork:
                # Check specific fork
                if ('mainnet' in pyspec and
                    fork in pyspec['mainnet'] and
                    pyspec_key in pyspec['mainnet'][fork] and
                    item_name in pyspec['mainnet'][fork][pyspec_key]):
                    item_found = True
            else:
                # Check if item exists in any fork
                if 'mainnet' in pyspec:
                    for check_fork in pyspec['mainnet']:
                        if (pyspec_key in pyspec['mainnet'][check_fork] and
                            item_name in pyspec['mainnet'][check_fork][pyspec_key]):
                            item_found = True
                            break

            if not item_found:
                fork_suffix = f"#{fork}" if fork else ""
                errors.append(f"invalid key: {exception_key}.{item_name}{fork_suffix}")

    if errors:
        error_msg = "Invalid exception items in configuration:\n" + "\n".join(f"  - {e}" for e in errors)
        raise Exception(error_msg)

def load_config(directory=None):
    """
    Load configuration from .ethspecify.yml file in the specified directory.
    Returns a dict with configuration values, or empty dict if no config file found.
    """
    if directory is None:
        directory = os.getcwd()

    config_path = os.path.join(directory, '.ethspecify.yml')

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
                if not config:
                    return {}

                # Get version from config, default to 'nightly'
                version = config.get('version', 'nightly')

                specrefs_require = False
                if 'specrefs' in config and isinstance(config['specrefs'], dict):
                    specrefs_require = config['specrefs'].get('require_exceptions_have_fork', False)
                require_exceptions_have_fork = config.get('require_exceptions_have_fork', False) or specrefs_require

                # Validate exceptions in root config
                if 'exceptions' in config:
                    validate_exception_items(config['exceptions'], version, require_exceptions_have_fork)

                # Also validate exceptions in specrefs section if present
                if 'specrefs' in config and isinstance(config['specrefs'], dict):
                    if 'exceptions' in config['specrefs']:
                        validate_exception_items(config['specrefs']['exceptions'], version, require_exceptions_have_fork)

                return config
        except (yaml.YAMLError, IOError) as e:
            print(f"Warning: Error reading .ethspecify.yml file: {e}")
            return {}

    return {}


def is_excepted(item_name, fork, exceptions):
    """
    Check if an item#fork combination is in the exception list.
    Exceptions can be:
    - Just the item name (applies to all forks)
    - item#fork (specific fork)
    """
    if not exceptions:
        return False

    # Check for exact match with fork
    if f"{item_name}#{fork}" in exceptions:
        return True

    # Check for item name only (all forks)
    if item_name in exceptions:
        return True

    return False


def strip_comments(code):
    # Split the original code into lines so we can decide which to keep or skip
    code_lines = code.splitlines(True)  # Keep line endings in each element

    # Dictionary: line_index -> list of (column, token_string)
    non_comment_tokens = {}

    # Tokenize the entire code
    tokens = tokenize.generate_tokens(io.StringIO(code).readline)
    for ttype, tstring, (srow, scol), _, _ in tokens:
        # Skip comments and pure newlines
        if ttype == tokenize.COMMENT:
            continue
        if ttype in (tokenize.NEWLINE, tokenize.NL):
            continue
        # Store all other tokens, adjusting line index to be zero-based
        non_comment_tokens.setdefault(srow - 1, []).append((scol, tstring))

    final_lines = []
    # Reconstruct or skip lines
    for i, original_line in enumerate(code_lines):
        # If the line has no non-comment tokens
        if i not in non_comment_tokens:
            # Check whether the original line is truly blank (just whitespace)
            if original_line.strip():
                # The line wasn't empty => it was a comment-only line, so skip it
                continue
            else:
                # A truly empty/blank line => keep it
                final_lines.append("")
        else:
            # Reconstruct this line from the stored tokens (preserving indentation/spaces)
            tokens_for_line = sorted(non_comment_tokens[i], key=lambda x: x[0])
            line_str = ""
            last_col = 0
            for col, token_str in tokens_for_line:
                # Insert spaces if there's a gap
                if col > last_col:
                    line_str += " " * (col - last_col)
                line_str += token_str
                last_col = col + len(token_str)
            # Strip trailing whitespace at the end of the line
            final_lines.append(line_str.rstrip())

    return "\n".join(final_lines)


def grep(root_directory, search_pattern, excludes=[]):
    matched_files = []
    regex = re.compile(search_pattern)
    exclude_patterns = [re.compile(pattern) for pattern in excludes]
    for dirpath, _, filenames in os.walk(root_directory):
        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            if any(pattern.search(file_path) for pattern in exclude_patterns):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    if any(regex.search(line) for line in file):
                        matched_files.append(file_path)
            except (UnicodeDecodeError, IOError):
                continue
    return matched_files


def diff(a_name, a_content, b_name, b_content):
    diff = difflib.unified_diff(
        a_content.splitlines(), b_content.splitlines(),
        fromfile=a_name, tofile=b_name, lineterm=""
    )
    return "\n".join(diff)


@functools.lru_cache()
def get_pyspec(version="nightly"):
    url = f"https://raw.githubusercontent.com/ethereum/ethspecify/main/pyspec/{version}/pyspec.json"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def get_previous_forks(fork, version="nightly"):
    pyspec = get_pyspec(version)
    config_vars = pyspec["mainnet"][fork]["config_vars"]
    previous_forks = ["phase0"]
    for key in config_vars.keys():
        if key.endswith("_FORK_VERSION"):
            if key != f"{fork.upper()}_FORK_VERSION":
                if key != "GENESIS_FORK_VERSION":
                    f = key.split("_")[0].lower()
                    # Skip EIP forks
                    if not f.startswith("eip"):
                        previous_forks.append(f)
    return list(reversed(previous_forks))


def get_spec(attributes, preset, fork, version="nightly"):
    pyspec = get_pyspec(version)
    spec = None
    if "function" in attributes or "fn" in attributes:
        if "function" in attributes and "fn" in attributes:
            raise Exception(f"cannot contain 'function' and 'fn'")
        if "function" in attributes:
            function_name = attributes["function"]
        else:
            function_name = attributes["fn"]

        spec = pyspec[preset][fork]["functions"][function_name]
        spec_lines = spec.split("\n")
        start, end = None, None

        try:
            vars = attributes["lines"].split("-")
            if len(vars) == 1:
                start = min(len(spec_lines), max(1, int(vars[0])))
                end = start
            elif len(vars) == 2:
                start = min(len(spec_lines), max(1, int(vars[0])))
                end = max(1, min(len(spec_lines), int(vars[1])))
            else:
                raise Exception(f"Invalid lines range for {function_name}: {attributes['lines']}")
        except KeyError:
            pass

        if start or end:
            start = start or 1
            if start > end:
                raise Exception(f"Invalid lines range for {function_name}: ({start}, {end})")
            # Subtract one because line numbers are one-indexed
            spec = "\n".join(spec_lines[start-1:end])
            spec = textwrap.dedent(spec)

    elif "constant_var" in attributes:
        if spec is not None:
            raise Exception(f"Tag can only specify one spec item")
        info = pyspec[preset][fork]["constant_vars"][attributes["constant_var"]]
        spec = (
            attributes["constant_var"]
            + (": " + info[0] if info[0] is not None else "")
            + " = "
            + info[1]
        )
    elif "preset_var" in attributes:
        if spec is not None:
            raise Exception(f"Tag can only specify one spec item")
        info = pyspec[preset][fork]["preset_vars"][attributes["preset_var"]]
        spec = (
            attributes["preset_var"]
            + (": " + info[0] if info[0] is not None else "")
            + " = "
            + info[1]
        )
    elif "config_var" in attributes:
        if spec is not None:
            raise Exception(f"Tag can only specify one spec item")
        info = pyspec[preset][fork]["config_vars"][attributes["config_var"]]
        spec = (
            attributes["config_var"]
            + (": " + info[0] if info[0] is not None else "")
            + " = "
            + info[1]
        )
    elif "custom_type" in attributes:
        if spec is not None:
            raise Exception(f"Tag can only specify one spec item")
        spec = (
            attributes["custom_type"]
            + " = "
            + pyspec[preset][fork]["custom_types"][attributes["custom_type"]]
        )
    elif "ssz_object" in attributes or "container" in attributes:
        if spec is not None:
            raise Exception(f"Tag can only specify one spec item")
        if "ssz_object" in attributes and "container" in attributes:
            raise Exception(f"cannot contain 'ssz_object' and 'container'")
        if "ssz_object" in attributes:
            object_name = attributes["ssz_object"]
        else:
            object_name = attributes["container"]
        spec = pyspec[preset][fork]["ssz_objects"][object_name]
    elif "dataclass" in attributes:
        if spec is not None:
            raise Exception(f"Tag can only specify one spec item")
        spec = pyspec[preset][fork]["dataclasses"][attributes["dataclass"]].replace("@dataclass\n", "")
    else:
        raise Exception("invalid spec tag")
    return spec

def get_latest_fork(version="nightly"):
    """A helper function to get the latest non-eip fork."""
    pyspec = get_pyspec(version)
    forks = sorted(
        [fork for fork in pyspec["mainnet"].keys() if not fork.startswith("eip")],
        key=lambda x: (x != "phase0", x)
    )
    return forks[-1] if forks else "phase0"


def get_spec_item_changes(fork, preset="mainnet", version="nightly"):
    """
    Compare spec items in the given fork with previous forks to detect changes.
    Returns dict with categories containing items marked as (new) or (modified).
    """
    pyspec = get_pyspec(version)
    if fork not in pyspec[preset]:
        raise ValueError(f"Fork '{fork}' not found in {preset} preset")

    current_fork_data = pyspec[preset][fork]
    previous_forks = get_previous_forks(fork, version)

    changes = {
        'functions': {},
        'constant_vars': {},
        'custom_types': {},
        'ssz_objects': {},
        'dataclasses': {},
        'preset_vars': {},
        'config_vars': {},
    }

    # Check each category of spec items
    for category in changes.keys():
        if category not in current_fork_data:
            continue

        for item_name, item_content in current_fork_data[category].items():
            status = _get_item_status(item_name, item_content, category, previous_forks, pyspec, preset)
            if status:
                changes[category][item_name] = status

    return changes


def _get_item_status(item_name, current_content, category, previous_forks, pyspec, preset):
    """
    Determine if an item is new or modified compared to previous forks.
    Returns 'new', 'modified', or None if unchanged.
    """
    # Check if item exists in any previous fork
    found_in_previous = False
    previous_content = None

    for prev_fork in previous_forks:
        if (prev_fork in pyspec[preset] and
            category in pyspec[preset][prev_fork] and
            item_name in pyspec[preset][prev_fork][category]):

            found_in_previous = True
            prev_content = pyspec[preset][prev_fork][category][item_name]

            # Compare content with immediate previous version
            if prev_content != current_content:
                return "modified"
            else:
                # Found unchanged version, so this is not new or modified
                return None

    # If not found in any previous fork, it's new
    if not found_in_previous:
        return "new"

    return None


def get_spec_item_history(preset="mainnet", version="nightly"):
    """
    Get the complete history of all spec items across all forks.
    Returns dict with categories containing items and their fork history.
    """
    pyspec = get_pyspec(version)
    if preset not in pyspec:
        raise ValueError(f"Preset '{preset}' not found")

    # Get all forks in chronological order, excluding EIP forks
    all_forks = sorted(
        [fork for fork in pyspec[preset].keys() if not fork.startswith("eip")],
        key=lambda x: (x != "phase0", x)
    )

    # Track all unique items across all forks
    all_items = {
        'functions': set(),
        'constant_vars': set(),
        'custom_types': set(),
        'ssz_objects': set(),
        'dataclasses': set(),
        'preset_vars': set(),
        'config_vars': set(),
    }

    # Collect all item names
    for fork in all_forks:
        if fork not in pyspec[preset]:
            continue
        fork_data = pyspec[preset][fork]
        for category in all_items.keys():
            if category in fork_data:
                all_items[category].update(fork_data[category].keys())

    # Build history for each item
    history = {}
    for category in all_items.keys():
        history[category] = {}
        for item_name in all_items[category]:
            item_history = _trace_item_history(item_name, category, all_forks, pyspec, preset)
            if item_history:
                history[category][item_name] = item_history

    return history


def _trace_item_history(item_name, category, all_forks, pyspec, preset):
    """
    Trace the history of a specific item across all forks.
    Returns a list of forks where the item was introduced or modified.
    """
    history_forks = []
    previous_content = None

    for fork in all_forks:
        if (fork in pyspec[preset] and
            category in pyspec[preset][fork] and
            item_name in pyspec[preset][fork][category]):

            current_content = pyspec[preset][fork][category][item_name]

            if previous_content is None:
                # First appearance
                history_forks.append(fork)
            elif current_content != previous_content:
                # Content changed
                history_forks.append(fork)

            previous_content = current_content

    return history_forks

def parse_common_attributes(attributes, config=None):
    if config is None:
        config = {}

    try:
        preset = attributes["preset"]
    except KeyError:
        preset = "mainnet"

    try:
        version = attributes["version"]
    except KeyError:
        version = config.get("version", "nightly")

    try:
        fork = attributes["fork"]
    except KeyError:
        fork = get_latest_fork(version)

    try:
        style = attributes["style"]
    except KeyError:
        style = config.get("style", "hash")

    return preset, fork, style, version

def get_spec_item(attributes, config=None):
    preset, fork, style, version = parse_common_attributes(attributes, config)
    spec = get_spec(attributes, preset, fork, version)

    if style == "full" or style == "hash":
        return spec
    elif style == "diff":
        previous_forks = get_previous_forks(fork, version)

        previous_fork = None
        previous_spec = None
        for i, _ in enumerate(previous_forks):
            previous_fork = previous_forks[i]
            previous_spec = get_spec(attributes, preset, previous_fork, version)
            if previous_spec != "phase0":
                try:
                    previous_previous_fork = previous_forks[i+1]
                    previous_previous_spec = get_spec(attributes, preset, previous_previous_fork, version)
                    if previous_previous_spec == previous_spec:
                        continue
                except KeyError:
                    pass
                except IndexError:
                    pass
            if previous_spec != spec:
                break
            if previous_spec == "phase0":
                raise Exception("there is no previous spec for this")
        return diff(previous_fork, strip_comments(previous_spec), fork, strip_comments(spec))
    if style == "link":
        link = build_spec_link(attributes, fork, version)
        if link is None:
            return "Not available for this type of spec"
        return link
    else:
        raise Exception("invalid style type")


def build_spec_link(attributes, fork, version):
    base = f"https://ethspec.tools/#specs/{version}"
    if "function" in attributes or "fn" in attributes:
        if "function" in attributes and "fn" in attributes:
            raise Exception(f"cannot contain 'function' and 'fn'")
        function_name = attributes.get("function", attributes.get("fn"))
        return f"{base}/functions-{function_name}-{fork}"
    if "constant_var" in attributes:
        return f"{base}/constant_vars-{attributes['constant_var']}-{fork}"
    if "preset_var" in attributes:
        return f"{base}/preset_vars-{attributes['preset_var']}-{fork}"
    if "config_var" in attributes:
        return f"{base}/config_vars-{attributes['config_var']}-{fork}"
    if "custom_type" in attributes:
        return f"{base}/custom_types-{attributes['custom_type']}-{fork}"
    if "ssz_object" in attributes or "container" in attributes:
        if "ssz_object" in attributes and "container" in attributes:
            raise Exception(f"cannot contain 'ssz_object' and 'container'")
        object_name = attributes.get("ssz_object", attributes.get("container"))
        return f"{base}/ssz_objects-{object_name}-{fork}"
    if "dataclass" in attributes:
        return f"{base}/dataclasses-{attributes['dataclass']}-{fork}"
    return None


def extract_attributes(tag):
    attr_pattern = re.compile(r'(\w+)="(.*?)"')
    return dict(attr_pattern.findall(tag))


def sort_specref_yaml(yaml_file):
    """
    Sort specref entries in a YAML file by their name field.
    Preserves formatting and adds single blank lines between entries.
    """
    if not os.path.exists(yaml_file):
        return False

    try:
        with open(yaml_file, 'r') as f:
            content_str = f.read()

        # Extract search values that originally had single or double quotes before YAML parsing
        # This preserves the original quoting style exactly
        single_quoted_searches = set()
        for match in re.finditer(r"search:\s*'([^']*)'", content_str):
            single_quoted_searches.add(match.group(1))
        double_quoted_searches = set()
        for match in re.finditer(r'search:\s*"([^"]*)"', content_str):
            double_quoted_searches.add(match.group(1))

        # Temporarily quote unquoted search strings with colons so YAML can parse them
        # This doesn't affect the output - we restore original quoting when writing
        content_str = re.sub(r'(\s+search:\s+)([^"\'\n]+:)(\s*$)', r'\1"\2"\3', content_str, flags=re.MULTILINE)

        try:
            content = yaml.safe_load(content_str)
        except yaml.YAMLError:
            # Fall back to FullLoader if safe_load fails
            content = yaml.load(content_str, Loader=yaml.FullLoader)

        if not content:
            return False

        # Handle both array of objects and single object formats
        if isinstance(content, list):
            # Sort the list by 'name' field if it exists
            # Special handling for fork ordering within the same item
            def sort_key(x):
                name = x.get('name', '') if isinstance(x, dict) else str(x)

                # Known fork names for ordering
                forks = ['phase0', 'altair', 'bellatrix', 'capella', 'deneb', 'electra']

                # Check if name contains a # separator (like "slash_validator#phase0")
                if '#' in name:
                    base_name, fork = name.rsplit('#', 1)
                    # Define fork order based on known forks list
                    fork_lower = fork.lower()
                    if fork_lower in forks:
                        fork_order = forks.index(fork_lower)
                    else:
                        # Unknown forks go after known ones, sorted alphabetically
                        fork_order = len(forks)
                    return (base_name, fork_order, fork)
                else:
                    # Check if name ends with a fork name (like "BeaconStatePhase0")
                    name_lower = name.lower()
                    for i, fork in enumerate(forks):
                        if name_lower.endswith(fork):
                            # Extract base name
                            base_name = name[:-len(fork)]
                            return (base_name, i, name)

                    # No fork pattern found, just sort by name
                    return (name, 0, '')

            sorted_content = sorted(content, key=sort_key)

            # Custom YAML writing to preserve formatting
            output_lines = []
            for i, item in enumerate(sorted_content):
                if i > 0:
                    # Add a single blank line between entries
                    output_lines.append("")

                # Start each entry with a dash
                first_line = True
                for key, value in item.items():
                    if first_line:
                        prefix = "- "
                        first_line = False
                    else:
                        prefix = "  "

                    if key == 'spec':
                        # Preserve spec content as-is, using literal style
                        output_lines.append(f"{prefix}{key}: |")
                        # Indent the spec content - preserve it exactly as-is
                        spec_lines = value.rstrip().split('\n') if isinstance(value, str) else str(value).rstrip().split('\n')
                        for spec_line in spec_lines:
                            output_lines.append(f"    {spec_line}")
                    elif key == 'sources':
                        if isinstance(value, list) and len(value) == 0:
                            # Keep empty lists on the same line for clarity
                            output_lines.append(f"{prefix}{key}: []")
                        else:
                            output_lines.append(f"{prefix}{key}:")
                            if isinstance(value, list):
                                for source in value:
                                    if isinstance(source, dict):
                                        output_lines.append(f"    - file: {source.get('file', '')}")
                                        if 'search' in source:
                                            search_val = source['search']
                                            # Preserve original quoting style exactly
                                            if search_val in single_quoted_searches:
                                                search_val = f"'{search_val}'"
                                            elif search_val in double_quoted_searches:
                                                search_val = f'"{search_val}"'
                                            output_lines.append(f"      search: {search_val}")
                                        if 'regex' in source:
                                            # Keep boolean values lowercase for consistency
                                            regex_val = str(source['regex']).lower()
                                            output_lines.append(f"      regex: {regex_val}")
                                    else:
                                        output_lines.append(f"    - {source}")
                    else:
                        # Handle other fields - don't escape or modify the value
                        output_lines.append(f"{prefix}{key}: {value}")

            # Strip trailing whitespace from all lines
            output_lines = [line.rstrip() for line in output_lines]

            # Write back the sorted content
            with open(yaml_file, 'w') as f:
                f.write('\n'.join(output_lines))
                f.write('\n')  # End file with newline

            return True
        elif isinstance(content, dict):
            # If it's a single dict, we can't sort it
            return False
    except (yaml.YAMLError, IOError) as e:
        print(f"Error sorting {yaml_file}: {e}")
        return False

    return False

def replace_spec_tags(file_path, config=None):
    with open(file_path, 'r') as file:
        content = file.read()

    # Use provided config or load from file's directory as fallback
    if config is None:
        config = load_config(os.path.dirname(file_path))

    # Define regex to match self-closing tags and long (paired) tags separately
    pattern = re.compile(
        r'(?P<self><spec\b[^>]*\/>)|(?P<long><spec\b[^>]*>[\s\S]*?</spec>)',
        re.DOTALL
    )

    # Collect processed spec items for potential YAML updates
    processed_items = []

    def rebuild_opening_tag(attributes, hash_value):
        # Rebuild a fresh opening tag from attributes, overriding any existing hash.
        new_opening = "<spec"
        for key, val in attributes.items():
            if key != "hash":
                new_opening += f' {key}="{val}"'
        new_opening += f' hash="{hash_value}">'
        return new_opening

    def rebuild_self_closing_tag(attributes, hash_value):
        # Build a self-closing tag from attributes, forcing a single space before the slash.
        new_tag = "<spec"
        for key, val in attributes.items():
            if key != "hash":
                new_tag += f' {key}="{val}"'
        new_tag += f' hash="{hash_value}" />'
        return new_tag

    def replacer(match):
        # Always use the tag text from whichever group matched:
        if match.group("self") is not None:
            original_tag_text = match.group("self")
        else:
            original_tag_text = match.group("long")
        # Determine the original opening tag (ignore inner content)
        if match.group("self") is not None:
            original_tag_text = match.group("self")
        else:
            long_tag_text = match.group("long")
            opening_tag_match = re.search(r'<spec\b[^>]*>', long_tag_text)
            original_tag_text = opening_tag_match.group(0) if opening_tag_match else long_tag_text

        attributes = extract_attributes(original_tag_text)
        print(f"spec tag: {attributes}")
        preset, fork, style, version = parse_common_attributes(attributes, config)
        spec = get_spec(attributes, preset, fork, version)
        hash_value = hashlib.sha256(spec.encode('utf-8')).hexdigest()[:8]

        # Collect this item for potential YAML updates
        processed_items.append({
            'attributes': attributes,
            'preset': preset,
            'fork': fork,
            'style': style,
            'version': version,
            'spec': spec,
            'hash': hash_value
        })

        if style == "hash":
            # Rebuild a fresh self-closing tag.
            updated_tag = rebuild_self_closing_tag(attributes, hash_value)
            return updated_tag
        else:
            # For full/diff styles, rebuild as a long (paired) tag.
            new_opening = rebuild_opening_tag(attributes, hash_value)
            spec_content = get_spec_item(attributes, config)
            prefix = content[:match.start()].splitlines()[-1]
            prefixed_spec = "\n".join(
                f"{prefix}{line}" if line.rstrip() else prefix.rstrip()
                for line in spec_content.split("\n")
            )
            updated_tag = f"{new_opening}\n{prefixed_spec}\n{prefix}</spec>"
            return updated_tag


    # Replace all matches in the content
    updated_content = pattern.sub(replacer, content)

    # Write the updated content back to the file
    with open(file_path, 'w') as file:
        file.write(updated_content)

    # Return processed items for potential YAML updates
    return processed_items


def get_yaml_filename_for_spec_attr(spec_attr):
    """Map spec attribute to YAML filename."""
    attr_to_file = {
        'fn': 'functions.yml',
        'function': 'functions.yml',
        'constant_var': 'constants.yml',
        'config_var': 'configs.yml',
        'preset_var': 'presets.yml',
        'container': 'containers.yml',
        'ssz_object': 'containers.yml',
        'dataclass': 'dataclasses.yml',
        'custom_type': 'types.yml',
    }
    return attr_to_file.get(spec_attr)


def get_spec_attr_and_name(attributes):
    """Extract the spec attribute and item name from tag attributes."""
    spec_attrs = ['fn', 'function', 'constant_var', 'config_var', 'preset_var',
                  'container', 'ssz_object', 'dataclass', 'custom_type']
    for attr in spec_attrs:
        if attr in attributes:
            return attr, attributes[attr]
    return None, None


def load_yaml_entries(yaml_file):
    """Load existing entries from a YAML file."""
    if not os.path.exists(yaml_file):
        return []

    try:
        with open(yaml_file, 'r') as f:
            content_str = f.read()

        # Try to fix common YAML issues with unquoted search strings containing colons
        content_str = re.sub(r'(\s+search:\s+)([^"\n]+:)(\s*$)', r'\1"\2"\3', content_str, flags=re.MULTILINE)

        try:
            content = yaml.safe_load(content_str)
        except yaml.YAMLError:
            content = yaml.load(content_str, Loader=yaml.FullLoader)

        if isinstance(content, list):
            return content
        return []
    except (yaml.YAMLError, IOError):
        return []


def extract_spec_tag_key(spec_content, alias_groups=None, alias_preference=None):
    """Extract a unique key from spec tag to identify duplicates."""
    if not spec_content:
        return None

    # Extract the opening spec tag
    match = re.search(r'<spec\b([^>]*)>', spec_content)
    if not match:
        return None

    # Extract attributes from the tag
    attributes = extract_attributes(match.group(0))

    # Build a key from the spec attribute and fork
    # e.g., "constant_var:DOMAIN_PTC_ATTESTER:fork:gloas"
    key_parts = []
    for attr in ['fn', 'function', 'constant_var', 'config_var', 'preset_var',
                 'container', 'ssz_object', 'dataclass', 'custom_type']:
        if attr in attributes:
            normalized_attr = attr
            if alias_groups:
                for group_key, aliases in alias_groups.items():
                    if attr in aliases:
                        if alias_preference and group_key in alias_preference:
                            normalized_attr = alias_preference[group_key]
                        break
            key_parts.append(f"{normalized_attr}:{attributes[attr]}")
            break

    if 'fork' in attributes:
        key_parts.append(f"fork:{attributes['fork']}")

    return ':'.join(key_parts) if key_parts else None


def add_missing_entries_to_yaml(yaml_file, new_entries):
    """Add new entries to a YAML file and sort it."""
    if not new_entries:
        return

    # Extract search values that originally had single or double quotes before YAML parsing
    # This preserves the original quoting style exactly
    single_quoted_searches = set()
    double_quoted_searches = set()
    if os.path.exists(yaml_file):
        with open(yaml_file, 'r') as f:
            content_str = f.read()
        for match in re.finditer(r"search:\s*'([^']*)'", content_str):
            single_quoted_searches.add(match.group(1))
        for match in re.finditer(r'search:\s*"([^"]*)"', content_str):
            double_quoted_searches.add(match.group(1))

    # Load existing entries
    existing_entries = load_yaml_entries(yaml_file)

    # Build alias preferences from the first existing entries in the file
    alias_groups = {
        'function': ['fn', 'function'],
        'ssz_object': ['ssz_object', 'container'],
    }
    alias_preference = {}
    for entry in existing_entries:
        if not isinstance(entry, dict) or 'spec' not in entry:
            continue
        spec_content = entry.get('spec', '')
        match = re.search(r'<spec\b([^>]*)>', spec_content)
        if not match:
            continue
        attributes = extract_attributes(match.group(0))
        for group_key, aliases in alias_groups.items():
            if group_key in alias_preference:
                continue
            for alias in aliases:
                if alias in attributes:
                    alias_preference[group_key] = alias
                    break

    # Build a set of existing spec tag keys
    existing_spec_keys = set()
    for entry in existing_entries:
        if isinstance(entry, dict) and 'spec' in entry:
            spec_key = extract_spec_tag_key(entry['spec'], alias_groups, alias_preference)
            if spec_key:
                existing_spec_keys.add(spec_key)

    # Filter out entries that already exist (based on spec tag, not name)
    entries_to_add = []
    for entry in new_entries:
        spec_key = extract_spec_tag_key(entry.get('spec', ''), alias_groups, alias_preference)
        if spec_key and spec_key not in existing_spec_keys:
            entries_to_add.append(entry)
            existing_spec_keys.add(spec_key)  # Avoid duplicates within new entries

    if not entries_to_add:
        return

    # Combine and write
    all_entries = existing_entries + entries_to_add

    # Ensure directory exists
    os.makedirs(os.path.dirname(yaml_file) if os.path.dirname(yaml_file) else '.', exist_ok=True)

    # Write combined entries using the same format as generate_specref_files
    with open(yaml_file, 'w') as f:
        for i, entry in enumerate(all_entries):
            if i > 0:
                f.write('\n')
            f.write(f'- name: {entry["name"]}\n')
            if 'sources' in entry:
                if isinstance(entry['sources'], list) and len(entry['sources']) == 0:
                    f.write('  sources: []\n')
                else:
                    f.write('  sources:\n')
                    for source in entry['sources']:
                        if isinstance(source, dict):
                            f.write(f'    - file: {source.get("file", "")}\n')
                            if 'search' in source:
                                search_val = source["search"]
                                # Preserve original quoting style exactly
                                if search_val in single_quoted_searches:
                                    search_val = f"'{search_val}'"
                                elif search_val in double_quoted_searches:
                                    search_val = f'"{search_val}"'
                                f.write(f'      search: {search_val}\n')
                            if 'regex' in source:
                                f.write(f'      regex: {source["regex"]}\n')
                        else:
                            f.write(f'    - {source}\n')
            if 'spec' in entry:
                f.write('  spec: |\n')
                for line in entry['spec'].split('\n'):
                    f.write(f'    {line}\n')

    # Sort the file
    sort_specref_yaml(yaml_file)

    print(f"Added {len(entries_to_add)} new entries to {yaml_file}")


def check_source_files(yaml_file, project_root, exceptions=None):
    """
    Check that source files referenced in a YAML file exist and contain expected search strings.
    Returns (valid_count, total_count, errors)
    """
    if exceptions is None:
        exceptions = []
    if not os.path.exists(yaml_file):
        return 0, 0, [f"YAML file not found: {yaml_file}"]

    errors = []
    total_count = 0

    try:
        with open(yaml_file, 'r') as f:
            content_str = f.read()

        # Try to fix common YAML issues with unquoted search strings
        # Replace unquoted search values ending with colons
        content_str = re.sub(r'(\s+search:\s+)([^"\n]+:)(\s*$)', r'\1"\2"\3', content_str, flags=re.MULTILINE)

        try:
            content = yaml.safe_load(content_str)
        except yaml.YAMLError:
            # Fall back to FullLoader if safe_load fails
            content = yaml.load(content_str, Loader=yaml.FullLoader)
    except (yaml.YAMLError, IOError) as e:
        return 0, 0, [f"YAML parsing error in {yaml_file}: {e}"]

    if not content:
        return 0, 0, []

    # Handle both array of objects and single object formats
    items = content if isinstance(content, list) else [content]

    for item in items:
        if not isinstance(item, dict) or 'sources' not in item:
            continue

        # Extract spec reference information from the item
        spec_ref = None
        if 'spec' in item and isinstance(item['spec'], str):
            # Try to extract spec reference from spec content
            spec_content = item['spec']
            # Look for any spec tag attribute and fork
            spec_tag_match = re.search(r'<spec\s+([^>]+)>', spec_content)
            if spec_tag_match:
                tag_attrs = spec_tag_match.group(1)
                # Extract fork
                fork_match = re.search(r'fork="([^"]+)"', tag_attrs)
                # Extract the main attribute (not hash or fork)
                attr_matches = re.findall(r'(\w+)="([^"]+)"', tag_attrs)

                if fork_match:
                    fork = fork_match.group(1)
                    # Find the first non-meta attribute
                    for attr_name, attr_value in attr_matches:
                        if attr_name not in ['fork', 'hash', 'preset', 'version', 'style']:
                            # Map attribute names to type prefixes
                            type_map = {
                                'fn': 'functions',
                                'function': 'functions',
                                'constant_var': 'constants',
                                'config_var': 'configs',
                                'preset_var': 'presets',
                                'ssz_object': 'ssz_objects',
                                'container': 'ssz_objects',
                                'dataclass': 'dataclasses',
                                'custom_type': 'custom_types'
                            }
                            type_prefix = type_map.get(attr_name, attr_name)
                            spec_ref = f"{type_prefix}.{attr_value}#{fork}"
                            break

        # Fallback to just the name if spec extraction failed
        if not spec_ref and 'name' in item:
            spec_ref = item['name']

        # Extract item name and fork for exception checking
        item_name_for_exception = None
        fork_for_exception = None
        if spec_ref and '#' in spec_ref and '.' in spec_ref:
            # Format: "functions.item_name#fork"
            _, item_with_fork = spec_ref.split('.', 1)
            if '#' in item_with_fork:
                item_name_for_exception, fork_for_exception = item_with_fork.split('#', 1)

        # Check if sources list is empty
        if not item['sources']:
            if spec_ref:
                # Check if this item is in exceptions
                if item_name_for_exception and fork_for_exception:
                    if is_excepted(item_name_for_exception, fork_for_exception, exceptions):
                        total_count += 1
                        continue

                errors.append(f"EMPTY SOURCES: {spec_ref}")
            else:
                # Fallback if we can't extract spec reference
                item_name = item.get('name', 'unknown')
                errors.append(f"EMPTY SOURCES: No sources defined ({item_name})")
            total_count += 1
            continue

        # Check if item has non-empty sources but is in exceptions
        if item_name_for_exception and fork_for_exception:
            if is_excepted(item_name_for_exception, fork_for_exception, exceptions):
                errors.append(f"EXCEPTION CONFLICT: {spec_ref} has a specref")
                total_count += 1
                continue

        for source in item['sources']:
            # All sources now use the standardized dict format with file and optional search
            if not isinstance(source, dict) or 'file' not in source:
                continue

            file_path = source['file']
            search_string = source.get('search')
            is_regex = source.get('regex', False)

            total_count += 1

            # Parse line range from file path if present (#L123 or #L123-L456)
            line_range = None
            if '#L' in file_path:
                base_path, line_part = file_path.split('#L', 1)
                file_path = base_path
                # Format is always #L123 or #L123-L456, so just remove all 'L' characters
                line_range = line_part.replace('L', '')

            full_path = os.path.join(project_root, file_path)

            # Create error prefix with spec reference if available
            ref_prefix = f"{spec_ref} | " if spec_ref else ""

            # Check if file exists
            if not os.path.exists(full_path):
                errors.append(f"MISSING FILE: {ref_prefix}{file_path}")
                continue

            # Check line range if specified
            if line_range:
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        total_lines = len(lines)

                    # Parse line range
                    if '-' in line_range:
                        # Range like "123-456"
                        start_str, end_str = line_range.split('-', 1)
                        start_line = int(start_str)
                        end_line = int(end_str)

                        if start_line < 1 or end_line < 1 or start_line > end_line:
                            errors.append(f"INVALID LINE RANGE: {ref_prefix}#{line_range} - invalid range in {file_path}")
                            continue
                        elif end_line > total_lines:
                            errors.append(f"INVALID LINE RANGE: {ref_prefix}#{line_range} - line {end_line} exceeds file length ({total_lines}) in {file_path}")
                            continue
                    else:
                        # Single line like "123"
                        line_num = int(line_range)
                        if line_num < 1:
                            errors.append(f"INVALID LINE RANGE: {ref_prefix}#{line_range} - invalid line number in {file_path}")
                            continue
                        elif line_num > total_lines:
                            errors.append(f"INVALID LINE RANGE: {ref_prefix}#{line_range} - line {line_num} exceeds file length ({total_lines}) in {file_path}")
                            continue

                except ValueError:
                    errors.append(f"INVALID LINE RANGE: {ref_prefix}#{line_range} - invalid line format in {file_path}")
                    continue
                except (IOError, UnicodeDecodeError):
                    errors.append(f"ERROR READING: {ref_prefix}{file_path}")
                    continue

            # Check search string if provided
            if search_string:
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                        if is_regex:
                            # Use regex search
                            try:
                                pattern = re.compile(search_string, re.MULTILINE)
                                matches = list(pattern.finditer(content))
                                count = len(matches)
                                search_type = "REGEX"
                            except re.error as e:
                                errors.append(f"INVALID REGEX: {ref_prefix}'{search_string}' in {file_path} - {e}")
                                continue
                        else:
                            # Use literal string search
                            count = content.count(search_string)
                            search_type = "SEARCH"

                        if count == 0:
                            errors.append(f"{search_type} NOT FOUND: {ref_prefix}'{search_string}' in {file_path}")
                        elif count > 1:
                            errors.append(f"AMBIGUOUS {search_type}: {ref_prefix}'{search_string}' found {count} times in {file_path}")
                except (IOError, UnicodeDecodeError):
                    errors.append(f"ERROR READING: {ref_prefix}{file_path}")

    valid_count = total_count - len(errors)
    return valid_count, total_count, errors


def extract_spec_tags_from_yaml(yaml_file, tag_type=None):
    """
    Extract spec tags from a YAML file and return (tag_types_found, item#fork pairs).
    If tag_type is provided, only extract tags of that type.
    """
    if not os.path.exists(yaml_file):
        return set(), set()

    pairs = set()
    tag_types_found = set()

    # Known tag type attributes
    tag_attributes = ['fn', 'function', 'constant_var', 'config_var', 'preset_var',
                      'ssz_object', 'container', 'dataclass', 'custom_type']

    try:
        with open(yaml_file, 'r') as f:
            content_str = f.read()

        # Try to fix common YAML issues with unquoted search strings
        # Replace unquoted search values ending with colons
        content_str = re.sub(r'(\s+search:\s+)([^"\n]+:)(\s*$)', r'\1"\2"\3', content_str, flags=re.MULTILINE)

        try:
            content = yaml.safe_load(content_str)
        except yaml.YAMLError:
            # Fall back to FullLoader if safe_load fails
            content = yaml.load(content_str, Loader=yaml.FullLoader)

        if not content:
            return tag_types_found, pairs

        # Handle both array of objects and single object formats
        items = content if isinstance(content, list) else [content]

        for item in items:
            if not isinstance(item, dict) or 'spec' not in item:
                continue

            spec_content = item['spec']
            if not isinstance(spec_content, str):
                continue

            # Find all spec tags in the content
            spec_tag_pattern = r'<spec\s+([^>]+)>'
            spec_matches = re.findall(spec_tag_pattern, spec_content)

            for tag_attrs_str in spec_matches:
                # Extract all attributes from the tag
                attrs = dict(re.findall(r'(\w+)="([^"]+)"', tag_attrs_str))

                # Find which tag type this is
                found_tag_type = None
                item_name = None

                for attr in tag_attributes:
                    if attr in attrs:
                        found_tag_type = attr
                        item_name = attrs[attr]
                        # Normalize function to fn
                        if found_tag_type == 'function':
                            found_tag_type = 'fn'
                        # Normalize container to ssz_object
                        if found_tag_type == 'container':
                            found_tag_type = 'ssz_object'
                        break

                if found_tag_type and 'fork' in attrs:
                    tag_types_found.add(found_tag_type)

                    # If tag_type filter is specified, only add matching types
                    if tag_type is None or tag_type == found_tag_type:
                        pairs.add(f"{item_name}#{attrs['fork']}")

    except (IOError, UnicodeDecodeError, yaml.YAMLError):
        pass

    return tag_types_found, pairs


def generate_specrefs_from_files(files_with_spec_tags, project_dir):
    """
    Generate specrefs data from files containing spec tags.
    Returns a dict with spec tag info and their source locations.
    """
    specrefs = {}

    for file_path in files_with_spec_tags:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Find all spec tags in the file
            spec_tag_pattern = r'<spec\s+([^>]+?)(?:\s*/>|>)'
            matches = re.finditer(spec_tag_pattern, content)

            for match in matches:
                tag_attrs_str = match.group(1)
                attrs = extract_attributes(f"<spec {tag_attrs_str}>")

                # Determine the spec type and name
                spec_type = None
                spec_name = None
                fork = attrs.get('fork', None)

                # Check each possible spec attribute
                for attr_name in ['fn', 'function', 'constant_var', 'config_var',
                                  'preset_var', 'ssz_object', 'container', 'dataclass', 'custom_type']:
                    if attr_name in attrs:
                        spec_type = attr_name
                        spec_name = attrs[attr_name]
                        break

                if spec_type and spec_name:
                    # Normalize container to ssz_object for consistency
                    if spec_type == 'container':
                        spec_type = 'ssz_object'
                    # Create a unique key for this spec reference
                    key = f"{spec_type}.{spec_name}"
                    if fork:
                        key += f"#{fork}"

                    if key not in specrefs:
                        specrefs[key] = {
                            'name': spec_name,
                            'type': spec_type,
                            'fork': fork,
                            'sources': []
                        }

                    # Add this source location
                    rel_path = os.path.relpath(file_path, project_dir)

                    # Get line number of the match
                    lines_before = content[:match.start()].count('\n')
                    line_num = lines_before + 1

                    specrefs[key]['sources'].append({
                        'file': rel_path,
                        'line': line_num
                    })

        except (IOError, UnicodeDecodeError):
            continue

    return specrefs


def process_generated_specrefs(specrefs, exceptions, version):
    """
    Process the generated specrefs and check coverage.
    Returns (success, results)
    """
    results = {}
    overall_success = True

    # Group specrefs by type for coverage checking
    specrefs_by_type = {}
    for _, data in specrefs.items():
        spec_type = data['type']
        if spec_type not in specrefs_by_type:
            specrefs_by_type[spec_type] = []
        specrefs_by_type[spec_type].append(data)

    # Map spec types to history keys
    type_to_history_key = {
        'fn': 'functions',
        'function': 'functions',
        'constant_var': 'constant_vars',
        'config_var': 'config_vars',
        'preset_var': 'preset_vars',
        'ssz_object': 'ssz_objects',
        'container': 'ssz_objects',
        'dataclass': 'dataclasses',
        'custom_type': 'custom_types'
    }

    # Map to exception keys
    type_to_exception_key = {
        'fn': 'functions',
        'function': 'functions',
        'constant_var': 'constants',
        'config_var': 'configs',
        'preset_var': 'presets',
        'ssz_object': 'ssz_objects',
        'container': 'ssz_objects',
        'dataclass': 'dataclasses',
        'custom_type': 'custom_types'
    }

    # Get spec history for coverage checking
    history = get_spec_item_history("mainnet", version)

    # Check coverage for each type
    total_found = 0
    total_expected = 0
    all_missing = []

    for spec_type, items in specrefs_by_type.items():
        history_key = type_to_history_key.get(spec_type, spec_type)
        exception_key = type_to_exception_key.get(spec_type, spec_type)

        # Get exceptions for this type - handle both singular and plural keys
        type_exceptions = []
        if exception_key in exceptions:
            type_exceptions = exceptions[exception_key]
        # Also check plural forms
        elif exception_key + 's' in exceptions:
            type_exceptions = exceptions[exception_key + 's']
        # Check if singular form exists when we have plural
        elif exception_key.endswith('s') and exception_key[:-1] in exceptions:
            type_exceptions = exceptions[exception_key[:-1]]

        # Special handling for ssz_objects/containers
        if spec_type in ['ssz_object', 'container'] and not type_exceptions:
            # Check for 'containers' as an alternative key
            if 'containers' in exceptions:
                type_exceptions = exceptions['containers']
            elif 'container' in exceptions:
                type_exceptions = exceptions['container']

        # Build set of what we found
        found_items = set()
        for item in items:
            if item['fork']:
                found_items.add(f"{item['name']}#{item['fork']}")
            else:
                # If no fork specified, we need to check all forks
                if history_key in history and item['name'] in history[history_key]:
                    for fork in history[history_key][item['name']]:
                        found_items.add(f"{item['name']}#{fork}")

        # Check what's expected
        if history_key in history:
            for item_name, forks in history[history_key].items():
                for fork in forks:
                    expected_key = f"{item_name}#{fork}"
                    total_expected += 1

                    # Check if excepted
                    if is_excepted(item_name, fork, type_exceptions):
                        total_found += 1
                        continue

                    if expected_key in found_items:
                        total_found += 1
                    else:
                        # Use the proper type prefix for the missing item
                        type_prefix_map = {
                            'functions': 'functions',
                            'constant_vars': 'constants',
                            'config_vars': 'configs',
                            'preset_vars': 'presets',
                            'ssz_objects': 'ssz_objects',
                            'dataclasses': 'dataclasses',
                            'custom_types': 'custom_types'
                        }
                        prefix = type_prefix_map.get(history_key, history_key)
                        all_missing.append(f"{prefix}.{expected_key}")

    # Count total spec references found
    total_refs = len(specrefs)

    # Store results
    results['Project Coverage'] = {
        'source_files': {
            'valid': total_refs,
            'total': total_refs,
            'errors': []
        },
        'coverage': {
            'found': total_found,
            'expected': total_expected,
            'missing': all_missing
        }
    }

    if all_missing:
        overall_success = False

    return overall_success, results


def check_coverage(yaml_file, tag_type, exceptions, preset="mainnet", version="nightly"):
    """
    Check that all spec items from ethspecify have corresponding tags in the YAML file.
    Returns (found_count, total_count, missing_items)
    """
    # Map tag types to history keys
    history_key_map = {
        'ssz_object': 'ssz_objects',
        'container': 'ssz_objects',
        'config_var': 'config_vars',
        'preset_var': 'preset_vars',
        'dataclass': 'dataclasses',
        'fn': 'functions',
        'constant_var': 'constant_vars',
        'custom_type': 'custom_types'
    }

    # Get expected items from ethspecify
    history = get_spec_item_history(preset, version)
    expected_pairs = set()

    history_key = history_key_map.get(tag_type, tag_type)
    if history_key in history:
        for item_name, forks in history[history_key].items():
            for fork in forks:
                expected_pairs.add(f"{item_name}#{fork}")

    # Get actual pairs from YAML file
    _, actual_pairs = extract_spec_tags_from_yaml(yaml_file, tag_type)

    # Find missing items (excluding exceptions)
    missing_items = []
    total_count = len(expected_pairs)

    for item_fork in expected_pairs:
        item_name, fork = item_fork.split('#', 1)

        if is_excepted(item_name, fork, exceptions):
            continue

        if item_fork not in actual_pairs:
            missing_items.append(item_fork)

    found_count = total_count - len(missing_items)
    return found_count, total_count, missing_items


def run_checks(project_dir, config):
    """
    Run all checks based on the configuration.
    Returns (success, results)
    """
    results = {}
    overall_success = True

    # Get version from config
    version = config.get('version', 'nightly')

    # Get specrefs config
    specrefs_config = config.get('specrefs', {})

    # Handle both old format (specrefs as array) and new format (specrefs as dict)
    if isinstance(specrefs_config, list):
        # Old format: specrefs: [file1, file2, ...]
        specrefs_files = specrefs_config
        exceptions = config.get('exceptions', {})
    else:
        # New format: specrefs: { files: [...], exceptions: {...} }
        specrefs_files = specrefs_config.get('files', [])

        # Support exceptions in either specrefs section or root, but not both
        specrefs_exceptions = specrefs_config.get('exceptions', {})
        root_exceptions = config.get('exceptions', {})

        if specrefs_exceptions and root_exceptions:
            print("Warning: Exceptions found in both root and specrefs sections. Using specrefs exceptions.")
            exceptions = specrefs_exceptions
        elif specrefs_exceptions:
            exceptions = specrefs_exceptions
        else:
            exceptions = root_exceptions

    # If no files specified, search the whole project for spec tags
    if not specrefs_files:
        print("No specific files configured, searching entire project for spec tags...")

        # Determine search root - configurable in specrefs section
        if 'search_root' in specrefs_config:
            # Use configured search_root (relative to project_dir)
            search_root_rel = specrefs_config['search_root']
            search_root = os.path.join(project_dir, search_root_rel) if not os.path.isabs(search_root_rel) else search_root_rel
            search_root = os.path.abspath(search_root)
        else:
            # Default behavior: if we're in a specrefs directory, search in the parent directory
            search_root = os.path.dirname(project_dir) if os.path.basename(project_dir) == 'specrefs' else project_dir

        print(f"Searching for spec tags in: {search_root}")

        # Use grep to find all files containing spec tags
        files_with_spec_tags = grep(search_root, r'<spec\b[^>]*>', [])

        if not files_with_spec_tags:
            print(f"No files with spec tags found in the project")
            return True, {}

        # Generate in-memory specrefs data from the found spec tags
        all_specrefs = generate_specrefs_from_files(files_with_spec_tags, search_root)

        # Process the generated specrefs
        return process_generated_specrefs(all_specrefs, exceptions, version)


    # Map tag types to exception keys (support both singular and plural)
    exception_key_map = {
        'ssz_object': ['ssz_objects', 'ssz_object', 'containers', 'container'],
        'container': ['ssz_objects', 'ssz_object', 'containers', 'container'],
        'config_var': ['configs', 'config_variables', 'config_var'],
        'preset_var': ['presets', 'preset_variables', 'preset_var'],
        'dataclass': ['dataclasses', 'dataclass'],
        'fn': ['functions', 'fn'],
        'constant_var': ['constants', 'constant_variables', 'constant_var'],
        'custom_type': ['custom_types', 'custom_type']
    }

    # Use explicit file list only
    for filename in specrefs_files:
        yaml_path = os.path.join(project_dir, filename)

        if not os.path.exists(yaml_path):
            print(f"Error: File {filename} defined in config but not found")
            overall_success = False
            continue

        # Detect tag types in the file
        tag_types_found, _ = extract_spec_tags_from_yaml(yaml_path)

        # Check for preset indicators in filename
        preset = "mainnet"  # default preset
        if 'minimal' in filename.lower():
            preset = "minimal"

        # Process each tag type found in the file
        if not tag_types_found:
            # No spec tags found, still check source files
            # Determine source root - use search_root if configured, otherwise use default behavior
            if 'search_root' in specrefs_config:
                search_root_rel = specrefs_config['search_root']
                source_root = os.path.join(project_dir, search_root_rel) if not os.path.isabs(search_root_rel) else search_root_rel
                source_root = os.path.abspath(source_root)
            else:
                # Default behavior: if we're in a specrefs directory, search in the parent directory
                source_root = os.path.dirname(project_dir) if os.path.basename(project_dir) == 'specrefs' else project_dir

            valid_count, total_count, source_errors = check_source_files(yaml_path, source_root, [])

            # Store results using filename as section name
            section_name = filename.replace('.yml', '').replace('-', ' ').title()
            if preset != "mainnet":
                section_name += f" ({preset.title()})"

            results[section_name] = {
                'source_files': {
                    'valid': valid_count,
                    'total': total_count,
                    'errors': source_errors
                },
                'coverage': {
                    'found': 0,
                    'expected': 0,
                    'missing': []
                }
            }

            if source_errors:
                overall_success = False
        else:
            # Process each tag type separately for better reporting
            all_missing_items = []
            total_found = 0
            total_expected = 0

            for tag_type in tag_types_found:
                # Get the appropriate exceptions for this tag type
                section_exceptions = []
                if tag_type in exception_key_map:
                    for key in exception_key_map[tag_type]:
                        if key in exceptions:
                            section_exceptions = exceptions[key]
                            break

                # Check coverage for this specific tag type
                found_count, expected_count, missing_items = check_coverage(yaml_path, tag_type, section_exceptions, preset, version)
                total_found += found_count
                total_expected += expected_count
                all_missing_items.extend(missing_items)

            # Check source files (only once per file, not per tag type)
            # Use the union of all exceptions for source file checking
            all_exceptions = []
            for tag_type in tag_types_found:
                if tag_type in exception_key_map:
                    for key in exception_key_map[tag_type]:
                        if key in exceptions:
                            all_exceptions.extend(exceptions[key])

            # Determine source root - use search_root if configured, otherwise use default behavior
            if 'search_root' in specrefs_config:
                search_root_rel = specrefs_config['search_root']
                source_root = os.path.join(project_dir, search_root_rel) if not os.path.isabs(search_root_rel) else search_root_rel
                source_root = os.path.abspath(source_root)
            else:
                # Default behavior: if we're in a specrefs directory, search in the parent directory
                source_root = os.path.dirname(project_dir) if os.path.basename(project_dir) == 'specrefs' else project_dir

            valid_count, total_count, source_errors = check_source_files(yaml_path, source_root, all_exceptions)

            # Store results using filename as section name
            section_name = filename.replace('.yml', '').replace('-', ' ').title()
            if preset != "mainnet":
                section_name += f" ({preset.title()})"

            results[section_name] = {
                'source_files': {
                    'valid': valid_count,
                    'total': total_count,
                    'errors': source_errors
                },
                'coverage': {
                    'found': total_found,
                    'expected': total_expected,
                    'missing': all_missing_items
                }
            }

            # Update overall success
            if source_errors or all_missing_items:
                overall_success = False

    return overall_success, results


def update_entry_names_in_yaml_files(project_dir, specrefs_files):
    """
    Update all entry names to use the format <spec_item>#<fork>.
    """
    for yaml_file in specrefs_files:
        yaml_path = os.path.join(project_dir, yaml_file)

        if not os.path.exists(yaml_path):
            continue

        # Extract search values that originally had single or double quotes before YAML parsing
        # This preserves the original quoting style exactly
        single_quoted_searches = set()
        double_quoted_searches = set()
        with open(yaml_path, 'r') as f:
            content_str = f.read()
        for match in re.finditer(r"search:\s*'([^']*)'", content_str):
            single_quoted_searches.add(match.group(1))
        for match in re.finditer(r'search:\s*"([^"]*)"', content_str):
            double_quoted_searches.add(match.group(1))

        # Load existing entries
        existing_entries = load_yaml_entries(yaml_path)
        if not existing_entries:
            continue

        updated = False
        for entry in existing_entries:
            if not isinstance(entry, dict) or 'spec' not in entry:
                continue

            # Extract spec tag attributes
            spec_content = entry['spec']
            match = re.search(r'<spec\b([^>]*)>', spec_content)
            if not match:
                continue

            attributes = extract_attributes(match.group(0))

            # Get the spec item name and fork
            spec_attr, item_name = get_spec_attr_and_name(attributes)
            fork = attributes.get('fork')

            if item_name and fork:
                # Build the expected name
                expected_name = f'{item_name}#{fork}'

                # Update if different
                if entry.get('name') != expected_name:
                    entry['name'] = expected_name
                    updated = True

        # Write back if updated
        if updated:
            with open(yaml_path, 'w') as f:
                for i, entry in enumerate(existing_entries):
                    if i > 0:
                        f.write('\n')
                    f.write(f'- name: {entry["name"]}\n')
                    if 'sources' in entry:
                        if isinstance(entry['sources'], list) and len(entry['sources']) == 0:
                            f.write('  sources: []\n')
                        else:
                            f.write('  sources:\n')
                            for source in entry['sources']:
                                if isinstance(source, dict):
                                    f.write(f'    - file: {source.get("file", "")}\n')
                                    if 'search' in source:
                                        search_val = source["search"]
                                        # Preserve original quoting style exactly
                                        if search_val in single_quoted_searches:
                                            search_val = f"'{search_val}'"
                                        elif search_val in double_quoted_searches:
                                            search_val = f'"{search_val}"'
                                        f.write(f'      search: {search_val}\n')
                                    if 'regex' in source:
                                        f.write(f'      regex: {source["regex"]}\n')
                                else:
                                    f.write(f'    - {source}\n')
                    if 'spec' in entry:
                        f.write('  spec: |\n')
                        for line in entry['spec'].split('\n'):
                            f.write(f'    {line}\n')

            # Sort the file
            sort_specref_yaml(yaml_path)
            print(f"Updated entry names in {yaml_file}")


def add_missing_spec_items_to_yaml_files(project_dir, config, specrefs_files):
    """
    Add missing spec items to existing YAML files.
    Ensures all spec items from the specification exist in YAML files with sources: []
    """
    version = config.get('version', 'nightly')
    preset = 'mainnet'  # Could make this configurable
    specrefs_config = config.get('specrefs', {})

    # Resolve exceptions (support root or specrefs section, but not both)
    if isinstance(specrefs_config, list):
        exceptions = config.get('exceptions', {})
    else:
        specrefs_exceptions = specrefs_config.get('exceptions', {})
        root_exceptions = config.get('exceptions', {})
        if specrefs_exceptions and root_exceptions:
            print("Warning: Exceptions found in both root and specrefs sections. Using specrefs exceptions.")
            exceptions = specrefs_exceptions
        elif specrefs_exceptions:
            exceptions = specrefs_exceptions
        else:
            exceptions = root_exceptions

    category_exception_keys = {
        'ssz_objects': ['ssz_objects', 'ssz_object', 'containers', 'container'],
        'config_vars': ['configs', 'config_variables', 'config_var'],
        'preset_vars': ['presets', 'preset_variables', 'preset_var'],
        'dataclasses': ['dataclasses', 'dataclass'],
        'functions': ['functions', 'fn'],
        'constant_vars': ['constants', 'constant_variables', 'constant_var'],
        'custom_types': ['custom_types', 'custom_type']
    }

    # Get all spec items
    pyspec = get_pyspec(version)
    if preset not in pyspec:
        print(f"Error: Preset '{preset}' not found")
        return

    # Get all forks in chronological order, excluding EIP forks
    all_forks = sorted(
        [fork for fork in pyspec[preset].keys() if not fork.startswith("eip")],
        key=lambda x: (x != "phase0", x)
    )

    # Map YAML filenames to category keys and spec attribute names
    filename_to_category = {
        'constants.yml': ('constant_vars', 'constant_var'),
        'configs.yml': ('config_vars', 'config_var'),
        'presets.yml': ('preset_vars', 'preset_var'),
        'functions.yml': ('functions', 'fn'),
        'containers.yml': ('ssz_objects', 'container'),
        'dataclasses.yml': ('dataclasses', 'dataclass'),
        'types.yml': ('custom_types', 'custom_type'),
    }
    alias_groups = {
        'functions': ['fn', 'function'],
        'ssz_objects': ['container', 'ssz_object'],
    }

    for yaml_file in specrefs_files:
        yaml_path = os.path.join(project_dir, yaml_file)
        yaml_basename = os.path.basename(yaml_file)

        if yaml_basename not in filename_to_category:
            continue

        category, spec_attr = filename_to_category[yaml_basename]

        # Prefer the first alias used in the file for this category.
        if category in alias_groups:
            existing_entries = load_yaml_entries(yaml_path)
            preferred_attr = None
            for entry in existing_entries:
                if not isinstance(entry, dict) or 'spec' not in entry:
                    continue
                match = re.search(r'<spec\b([^>]*)>', entry.get('spec', ''))
                if not match:
                    continue
                attributes = extract_attributes(match.group(0))
                for alias in alias_groups[category]:
                    if alias in attributes:
                        preferred_attr = alias
                        break
                if preferred_attr:
                    break
            if preferred_attr:
                spec_attr = preferred_attr
        type_exceptions = []
        if isinstance(exceptions, dict) and category in category_exception_keys:
            for key in category_exception_keys[category]:
                if key in exceptions:
                    type_exceptions = exceptions[key]
                    break
        if type_exceptions and not isinstance(type_exceptions, list):
            type_exceptions = [type_exceptions]

        # Collect all items in this category organized by name and fork
        items_by_name = {}
        for fork in all_forks:
            if fork not in pyspec[preset]:
                continue
            fork_data = pyspec[preset][fork]

            if category not in fork_data:
                continue

            for item_name, item_data in fork_data[category].items():
                if item_name not in items_by_name:
                    items_by_name[item_name] = []
                items_by_name[item_name].append((fork, item_data))

        # Build entries for missing items
        new_entries = []
        for item_name in sorted(items_by_name.keys()):
            forks_data = items_by_name[item_name]

            # Find all unique versions of this item (where content differs)
            versions = []  # List of (fork, item_data, spec_content)
            prev_content = None

            for fork, item_data in forks_data:
                # Build the spec content based on category
                if category == 'functions':
                    spec_content = item_data
                elif category in ['constant_vars', 'config_vars', 'preset_vars']:
                    # item_data is a list: [type, value, ...]
                    if isinstance(item_data, (list, tuple)) and len(item_data) >= 2:
                        type_info = item_data[0]
                        value = item_data[1]
                        if type_info:
                            spec_content = f"{item_name}: {type_info} = {value}"
                        else:
                            spec_content = f"{item_name} = {value}"
                    else:
                        spec_content = str(item_data)
                elif category == 'ssz_objects':
                    spec_content = item_data
                elif category == 'dataclasses':
                    spec_content = item_data.replace("@dataclass\n", "")
                elif category == 'custom_types':
                    # custom_types are simple type aliases: TypeName = SomeType
                    spec_content = f"{item_name} = {item_data}"
                else:
                    spec_content = str(item_data)

                # Only add this version if it's different from the previous one
                if prev_content is None or spec_content != prev_content:
                    versions.append((fork, item_data, spec_content))
                    prev_content = spec_content

            # Skip versions that are excepted for this item
            if type_exceptions:
                versions = [
                    (fork, item_data, spec_content)
                    for fork, item_data, spec_content in versions
                    if not is_excepted(item_name, fork, type_exceptions)
                ]

            if not versions:
                continue

            # Create entries based on number of unique versions
            use_fork_suffix = len(versions) > 1

            for idx, (fork, item_data, spec_content) in enumerate(versions):
                # Calculate hash of current version
                hash_value = hashlib.sha256(spec_content.encode('utf-8')).hexdigest()[:8]

                # Build spec tag
                spec_tag = f'<spec {spec_attr}="{item_name}" fork="{fork}" hash="{hash_value}">'

                # Create entry
                entry_name = f'{item_name}#{fork}' if use_fork_suffix else item_name
                entry = {
                    'name': entry_name,
                    'sources': [],
                    'spec': f'{spec_tag}\n{spec_content}\n</spec>'
                }
                new_entries.append(entry)

        # Add missing entries to the YAML file
        if new_entries:
            add_missing_entries_to_yaml(yaml_path, new_entries)


def generate_config_file(version="nightly"):
    """Generate a .ethspecify.yml config file in the current directory."""
    config_path = '.ethspecify.yml'
    with open(config_path, 'w') as f:
        f.write(f'version: {version}\n')
        f.write('style: full\n')
        f.write('\n')
        f.write('specrefs:\n')
        f.write('  search_root: .\n')
        f.write('  auto_standardize_names: true\n')
        f.write('  auto_add_missing_entries: true\n')
        f.write('  require_exceptions_have_fork: true\n')
        f.write('\n')
        f.write('  exceptions:\n')
        f.write('    # Add any exceptions here\n')

    # Strip trailing whitespace
    with open(config_path, 'r') as f:
        lines = f.readlines()
    with open(config_path, 'w') as f:
        for line in lines:
            f.write(line.rstrip() + '\n')


def generate_specref_files(output_dir, version="nightly", preset="mainnet"):
    """
    Generate specref YAML files without sources for manual mapping.
    Creates a basic directory structure with empty sources.
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    # Get all spec items
    pyspec = get_pyspec(version)
    if preset not in pyspec:
        raise ValueError(f"Preset '{preset}' not found")

    # Get all forks in chronological order, excluding EIP forks
    all_forks = sorted(
        [fork for fork in pyspec[preset].keys() if not fork.startswith("eip")],
        key=lambda x: (x != "phase0", x)
    )

    # Map history keys to file names and spec attribute names
    category_map = {
        'constant_vars': ('constants.yml', 'constant_var'),
        'config_vars': ('configs.yml', 'config_var'),
        'preset_vars': ('presets.yml', 'preset_var'),
        'functions': ('functions.yml', 'fn'),
        'ssz_objects': ('containers.yml', 'container'),
        'dataclasses': ('dataclasses.yml', 'dataclass'),
        'custom_types': ('types.yml', 'custom_type'),
    }

    # Collect all items organized by category
    items_by_category = {cat: {} for cat in category_map.keys()}

    for fork in all_forks:
        if fork not in pyspec[preset]:
            continue
        fork_data = pyspec[preset][fork]

        for category in items_by_category.keys():
            if category not in fork_data:
                continue

            for item_name, item_data in fork_data[category].items():
                # Track which forks have this item
                if item_name not in items_by_category[category]:
                    items_by_category[category][item_name] = []
                items_by_category[category][item_name].append((fork, item_data))

    # Generate YAML files for each category
    for category, (filename, spec_attr) in category_map.items():
        if not items_by_category[category]:
            continue

        output_path = os.path.join(output_dir, filename)
        entries = []

        # Sort items alphabetically
        for item_name in sorted(items_by_category[category].keys()):
            forks_data = items_by_category[category][item_name]

            # Find all unique versions of this item (where content differs)
            versions = []  # List of (fork, item_data, spec_content)
            prev_content = None

            for fork, item_data in forks_data:
                # Build the spec content based on category
                if category == 'functions':
                    spec_content = item_data
                elif category in ['constant_vars', 'config_vars', 'preset_vars']:
                    # item_data is a list: [type, value, ...]
                    if isinstance(item_data, (list, tuple)) and len(item_data) >= 2:
                        type_info = item_data[0]
                        value = item_data[1]
                        if type_info:
                            spec_content = f"{item_name}: {type_info} = {value}"
                        else:
                            spec_content = f"{item_name} = {value}"
                    else:
                        spec_content = str(item_data)
                elif category == 'ssz_objects':
                    spec_content = item_data
                elif category == 'dataclasses':
                    spec_content = item_data.replace("@dataclass\n", "")
                elif category == 'custom_types':
                    # custom_types are simple type aliases: TypeName = SomeType
                    spec_content = f"{item_name} = {item_data}"
                else:
                    spec_content = str(item_data)

                # Only add this version if it's different from the previous one
                if prev_content is None or spec_content != prev_content:
                    versions.append((fork, item_data, spec_content))
                    prev_content = spec_content

            # Create entries based on number of unique versions
            use_fork_suffix = len(versions) > 1

            for idx, (fork, item_data, spec_content) in enumerate(versions):
                # Calculate hash of current version
                hash_value = hashlib.sha256(spec_content.encode('utf-8')).hexdigest()[:8]

                # Build spec tag
                spec_tag = f'<spec {spec_attr}="{item_name}" fork="{fork}" hash="{hash_value}">'

                # Create entry
                entry_name = f'{item_name}#{fork}' if use_fork_suffix else item_name
                entry = {
                    'name': entry_name,
                    'sources': [],
                    'spec': f'{spec_tag}\n{spec_content}\n</spec>'
                }
                entries.append(entry)

        # Write YAML file
        if entries:
            with open(output_path, 'w') as f:
                for i, entry in enumerate(entries):
                    if i > 0:
                        f.write('\n')
                    f.write(f'- name: {entry["name"]}\n')
                    f.write('  sources: []\n')
                    f.write('  spec: |\n')
                    for line in entry['spec'].split('\n'):
                        f.write(f'    {line}\n')

    # Create .ethspecify.yml config file in current directory
    config_path = '.ethspecify.yml'
    with open(config_path, 'w') as f:
        f.write(f'version: {version}\n')
        f.write('style: full\n')
        f.write('\n')
        f.write('specrefs:\n')
        f.write('  search_root: .\n')
        f.write('  auto_standardize_names: true\n')
        f.write('  auto_add_missing_entries: true\n')
        f.write('  require_exceptions_have_fork: true\n')
        f.write('  files:\n')
        for category, (filename, _) in category_map.items():
            if items_by_category[category]:
                f.write(f'    - {output_dir}/{filename}\n')
        f.write('\n')
        f.write('  exceptions:\n')
        f.write('    # Add any exceptions here\n')

    # Strip trailing whitespace from all generated files
    all_files = [config_path]
    for category, (filename, _) in category_map.items():
        if items_by_category[category]:
            all_files.append(os.path.join(output_dir, filename))

    for file_path in all_files:
        with open(file_path, 'r') as f:
            lines = f.readlines()
        with open(file_path, 'w') as f:
            for line in lines:
                f.write(line.rstrip() + '\n')

    return list(category_map.values())
