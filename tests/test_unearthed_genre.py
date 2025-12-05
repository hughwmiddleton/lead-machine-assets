from bs4 import BeautifulSoup
import unearthed_common as uc


def test_parse_unearthed_genre_variants():
    parse = uc.parse_unearthed_genre

    assert parse("POP / ACOUSTIC / INDIE") == ("Pop", "POP / ACOUSTIC / INDIE")
    assert parse("r&b / soul") == ("R&B", "r&b / soul")
    assert parse("Hip Hop") == ("Hip Hop", "Hip Hop")
    assert parse("") == (None, None)
    assert parse(None) == (None, None)


def test_extracts_genre_from_hero_block():
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
    text = uc.extract_unearthed_genre_text(soup)
    assert text == "POP / ACOUSTIC"
