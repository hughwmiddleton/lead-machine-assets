import functools
import importlib.util
import pathlib

from bs4 import BeautifulSoup


@functools.lru_cache(maxsize=1)
def _load_lead_machine_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    module_path = root / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_unearthed_genre_variants():
    mod = _load_lead_machine_module()
    parse = mod.parse_unearthed_genre

    assert parse("POP / ACOUSTIC / INDIE") == ("Pop", "POP / ACOUSTIC / INDIE")
    assert parse("r&b / soul") == ("R&B", "r&b / soul")
    assert parse("Hip Hop") == ("Hip Hop", "Hip Hop")
    assert parse("") == (None, None)
    assert parse(None) == (None, None)


def test_extracts_genre_from_hero_block():
    mod = _load_lead_machine_module()
    html = """
    <div class="q0wzh">
      <div class="Fcccu">Artist</div>
      <div>
        <div class="ZF6HQ BB33h">
          <ul class="uoclS PARBR" data-component="List" role="list">
            <li data-component="ListItem"><span class="VptPb">POP</span></li>
            <li data-component="ListItem"><span class="VptPb">ACOUSTIC</span></li>
          </ul>
        </div>
      </div>
      <h1 class="osS_Q">Artist Name</h1>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    text = mod._unearthed_extract_genre_text(soup)
    assert text == "POP / ACOUSTIC"
