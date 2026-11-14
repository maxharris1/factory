import pytest
import yaml

from lib.load import UniqueKeyLoader, load_yaml

MALFORMED = [
    "broken: [\n",
    "a: 1\na: 2\n",
    "key: *missing\n",
    "? [a, b]\n: value\n",
    "? {a: 1}\n: value\n",
    "{[a]: 1}\n",
    "outer:\n  ? - a\n  : 1\n",
    "a: !!int zz\n",
    "a: !!float zz\n",
    "a: !!timestamp 2020-13-45\n",
    "a: !!timestamp zz\n",
    "a: !!bool zz\n",
    "a: !!int +\n",
    "a: 1" + "0" * 5000 + "\n",
    "[" * 20000 + "]" * 20000,
    "{a: " * 5000 + "1" + "}" * 5000,
]


@pytest.mark.parametrize("text", MALFORMED)
def test_malformed_yaml_raises_only_yaml_error(text: str) -> None:
    with pytest.raises(yaml.YAMLError):
        load_yaml(text)


SAFE_TAGS = sorted(tag for tag in UniqueKeyLoader.yaml_constructors if tag)
JUNK = ["zz", "+", "-", "0x", "0b", "0o", ":", "1:", "1:2:", "!", "=", "_", ".", "1e", "2020-", "2020-1-1T", "[a]", "{a: 1}"]


@pytest.mark.parametrize("tag", SAFE_TAGS)
@pytest.mark.parametrize("junk", JUNK)
def test_any_tag_on_any_junk_value_raises_only_yaml_error(tag: str, junk: str) -> None:
    try:
        load_yaml(f"a: !<{tag}> {junk}\n")
    except yaml.YAMLError:
        pass


def test_well_formed_yaml_still_loads() -> None:
    assert load_yaml("a: [1, 2]\nb: {c: d}\n") == {"a": [1, 2], "b": {"c": "d"}}
