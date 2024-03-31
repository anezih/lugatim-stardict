import gzip
import html
import json
import re
import subprocess
import tarfile
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property
from pathlib import Path

from icu import Locale, UnicodeString
from pyglossary.glossary_v2 import Glossary

BETIK_DY = Path(__file__).absolute().parent

class Unmunched:
    """
    https://github.com/anezih/HunspellWordForms adresinde yer alan programla üretilen
    Hunspell unmunched veri dosyalarına erişmeyi sağlayan sınıf.
    """
    def __init__(self, dosya_yolu: Path) -> None:
        self._sozluk: dict[str,list[str]] = self._unmunched(dosya_yolu)

    def _unmunched(self, dosya_yolu: Path) -> dict[str, list[str]]:
        if not dosya_yolu.exists():
            raise FileNotFoundError
        _sozluk = dict()
        with gzip.open(dosya_yolu, "rt", encoding="utf-8") as g:
            tr_unmunched = json.load(g)
        for _dict in tr_unmunched:
            for k,v in _dict.items():
                if k in _sozluk.keys():
                    _sozluk[k] += v["SFX"]
                else:
                    _sozluk[k] = v["SFX"]
        return _sozluk

    def __getitem__(self, sozcuk: str) -> list[str]:
        return self._sozluk.get(sozcuk, list())

class DuzeltmeImi:
    def __init__(self) -> None:
        self.tablo = str.maketrans(
            {
                "Â" : "A", "â" : "a",
                "Û" : "U", "û" : "u",
                "Î" : "İ", "î" : "i"
            }
        )

    def imleri_kaldir(self, metin: str) -> str:
        return metin.translate(self.tablo)

class IcuYardimci:
    def __init__(self) -> None:
        self.tr = Locale("TR")

    def tumu_kucuk_harf(self, metin: str) -> str:
        return str(
            UnicodeString(metin).toLower(self.tr)
        )

class SesDosyalari:
    def __init__(self, klasor_konumu: Path) -> None:
        self.klasor_konumu = klasor_konumu

    @cached_property
    def IdDosya(self) -> dict[int, Path]:
        tum_dosyalar = self.klasor_konumu.glob("*")
        return {
            int(x.stem) : x
            for x in tum_dosyalar
        }

    def __iter__(self):
        for p in self.IdDosya.values():
            yield p

    def __getitem__(self, _id: int) -> Path:
        return self.IdDosya.get(_id, None)

class HamGirdi:
    """JSON dosyasından gelen verinin bir kısmı."""
    def __init__(self, **kwargs) -> None:
        self.id: int = kwargs["id"]
        self.kelime: str = kwargs["kelime"].strip()
        self.anlam: str = kwargs["anlam"].strip()
        self.kelimeSiralama: str = kwargs["kelimeSiralama"].strip()
        self.wordSearch: str = kwargs["wordSearch"].strip()
        self.noHtml: str = kwargs["noHtml"].strip()

class Girdi:
    """Ham girdiyi Glossary biçimine hazırlayan sınıf."""
    def __init__(self, ham_girdi: HamGirdi, icu_yardimci: IcuYardimci,
                 duzeltme_imi: DuzeltmeImi, unmunched: Unmunched,
                 ses_dosyalari: SesDosyalari, ses_ekle: bool) -> None:
        self.ham_girdi = ham_girdi
        self.icu_yardimci = icu_yardimci
        self.duzeltme_imi = duzeltme_imi
        self.unmunched = unmunched
        self.ses_dosyalari = ses_dosyalari
        self.ses_ekle = ses_ekle

    @property
    def baslik(self) -> str:
        return self.ham_girdi.kelime

    @cached_property
    def ses_dosyasi_path(self) -> Path:
        return self.ses_dosyalari[self.ham_girdi.id]

    @property
    def diger_bicimler(self) -> set[str]:
        """
        Madde başı sözcüğün diğer biçimlerini hazırlayan metod. Bunlar aşağıdakileri içerir:
        - Niye hepsinin büyük harf olduğu anlaşılamayan madde başlarının küçük harfle
        yazılmış biçimi,
        - Aralarında tire bulunan madde başlarının ayrı durumları, bunların küçük harflileri,
        - Üsttekilerin, varsa, unmunched sözlüğünden gelen çekimli biçimleri,
        - Üsttekilerin, varsa, düzeltme imsiz biçimleri
        """
        kucuk_harf = self.icu_yardimci.tumu_kucuk_harf(self.ham_girdi.kelime)
        tireli = list()
        # Dikkat, normal tire değil.
        if " – " in self.ham_girdi.kelime:
            tireli = [x.strip() for x in self.ham_girdi.kelime.split(" – ")]
        tireli_kucuk_harf = [self.icu_yardimci.tumu_kucuk_harf(x) for x in tireli]
        tireli_diger_bicimler = list()
        for t in tireli:
            tireli_diger_bicimler.extend(
                self.unmunched[self.icu_yardimci.tumu_kucuk_harf(t)]
            )
        kucuk_harf_diger_bicimler = self.unmunched[kucuk_harf]
        digerleri: list[str] = [
            kucuk_harf,
            *kucuk_harf_diger_bicimler,
            *tireli,
            *tireli_kucuk_harf,
            *tireli_diger_bicimler
        ]
        digerleri_duzeltme_imsiz = [self.duzeltme_imi.imleri_kaldir(x) for x in digerleri]
        sonuc = set([*digerleri, *digerleri_duzeltme_imsiz])
        if self.ham_girdi.kelime in sonuc:
            sonuc.remove(self.ham_girdi.kelime)
        return sonuc

    @property
    def l_word(self) -> list[str]:
        """
        Glossary.newEntry() metodu için gerekli madde başı ve madde başının
        diğer biçimlerini tek bir dizi şeklinde döndüren metod.
        """
        return [self.ham_girdi.kelime, *self.diger_bicimler]

    @property
    def anlam(self) -> str:
        """
        - Varsa:
            - Göndermeleri StaDict biçimine çevirir.
            - Ses dosyasını HTML'e ekler.
        - Anlamı döndürür.
        """
        gonderme_re = re.findall("<a href='/s/(.*?)'>", self.ham_girdi.anlam)
        if gonderme_re:
            for g in gonderme_re:
                eski = f"<a href='/s/{g}'>"
                yeni = f"<a href='bword://{html.escape(g)}'>"
                self.ham_girdi.anlam = self.ham_girdi.anlam.replace(eski, yeni)
        # escape_re = re.findall("<span.*?>(.*?)</span>", self.ham_girdi.anlam)
        # if escape_re:
        #     for e in escape_re:
        #         self.ham_girdi.anlam = (
        #             self.ham_girdi.anlam
        #             .replace(e, html.escape(e))
        #         )
        if not self.ham_girdi.anlam.startswith("<p>") and not self.ham_girdi.anlam.endswith("</p>"):
            self.ham_girdi.anlam = f"<p>{self.ham_girdi.anlam}</p>"
        if self.ses_dosyasi_path and self.ses_ekle:
            _html_ses = f'<audio src="{self.ses_dosyasi_path.name}">🔊</audio><br>'
            self.ham_girdi.anlam = f"{_html_ses}{self.ham_girdi.anlam}"
        self.ham_girdi.anlam = (
            f'<link rel="stylesheet" href="bicem.css">{self.ham_girdi.anlam}'
            .replace("\n","")
            .replace("\t", "")
            .replace("\u000b", "")
            .replace(
                'span class="Arabic18"',
                'span lang="ar" class="Arabic18"'
            )
            .replace(
                "span class='Arabic18'",
                'span lang="ar" class=\'Arabic18\''
            )
            .strip()
        )
        return self.ham_girdi.anlam

class Kubbealti:
    """
    Sözlüğü çeşitli biçimlerde kaydedebilen sınıf.
    """
    def __init__(self, json_dosyasi: str, bSesleri_ekle: bool = True) -> None:
        self.json_dosyasi = json_dosyasi
        self.bSesleri_ekle = bSesleri_ekle

    def sozluk_json(self) -> list[dict[str,str|int]]:
        p = Path(self.json_dosyasi)
        if not p.exists():
            raise FileNotFoundError
        if p.name.endswith("tar.gz"):
            with tarfile.open(p, "r:gz") as g:
                dosyalar = g.getnames()
                _json = json.load(g.extractfile(dosyalar[0]))
                return _json
        elif p.name.endswith(".json"):
            with open(p, "r", encoding="utf-8") as j:
                return json.load(j)

    def ses_dosyalari_ekle(self, glossary: Glossary, ses_dosyalari: SesDosyalari) -> None:
        def ekle(ses: Path) -> None:
            glossary.addEntry(
                glossary.newDataEntry(
                    ses.name,
                    ses.read_bytes()
                )
            )
        with ThreadPoolExecutor() as executor:
            for ses in ses_dosyalari:
                executor.submit(ekle, ses)

    def css_ekle(self, glossary: Glossary) -> None:
        css = BETIK_DY / "dosyalar" / "bicem.css"
        if css.exists():
            glossary.addEntry(
                glossary.newDataEntry(
                    css.name,
                    css.read_bytes()
                )
            )

    def bos_glossary(self) -> Glossary:
        """
        Glossary nesnesini yaratan ve üst verisi eklenmiş şekilde
        döndüren metod.
        """
        Glossary.init()
        glossary = Glossary()
        glossary.setInfo("title", "Kubbealtı Lugatı")
        glossary.setInfo("author", "lugatim-stardict betiğini yazan: https://github.com/anezih")
        glossary.setInfo("description",
                         "Türkçe kökenli sözcüklerin kökenlerinin bir türlü bulunamadığı sözlük.")
        glossary.sourceLangName = "tr"
        glossary.targetLangName = "tr"
        return glossary

    def glossary(self) -> Glossary:
        """
        Girdileri, biçem dosyasını, ses dosyalarını Glossary
        nesnesine ekleyen ve çeşitli biçimlerde kaydeden metod.
        """
        unmunched = Unmunched(BETIK_DY / "dosyalar" / "tr_TR.json.gz")
        icu_yardimci = IcuYardimci()
        duzeltme_imi = DuzeltmeImi()
        ses_dosyalari = SesDosyalari(BETIK_DY / "dosyalar" / "sesler")
        ham_girdiler = [HamGirdi(**x) for x in self.sozluk_json()]
        girdiler = [Girdi(x, icu_yardimci, duzeltme_imi, unmunched, ses_dosyalari, self.bSesleri_ekle)
                    for x in ham_girdiler]
        # https://github.com/huzheng001/stardict-3/blob/96b96d89eab5f0ad9246c2569a807d6d7982aa84/dict/doc/StarDictFileFormat#L191
        # Girdiler yukarıdaki bağlantıda yer alan metodla uyumlu olacak şekilde sıralanmalı.
        girdiler.sort(key=lambda x: (x.baslik.encode("utf-8").lower(), x.baslik))
        glossary = self.bos_glossary()
        for girdi in girdiler:
            glossary.addEntry(
                glossary.newEntry(
                    girdi.l_word,
                    girdi.anlam,
                    defiFormat="h"
                )
            )
        self.css_ekle(glossary)
        if self.bSesleri_ekle:
            self.ses_dosyalari_ekle(glossary, ses_dosyalari)
        return glossary

    def stardict(self) -> None:
        glossary = self.glossary()
        seslendirmesiz = "_Seslendirmesiz" if not self.bSesleri_ekle else ""
        klasor = BETIK_DY / f"KubbealtiLugati_StarDict{seslendirmesiz}"
        dosya_ismi = klasor / "KubbealtiLugati"
        if not klasor.exists():
            klasor.mkdir()
        glossary.write(str(dosya_ismi), "Stardict", dictzip=False)

    def json(self) -> None:
        self.bSesleri_ekle = False
        glossary = self.glossary()
        klasor = BETIK_DY / "KubbealtiLugati_Json"
        dosya_ismi = klasor / "KubbealtiLugati.json"
        if not klasor.exists():
            klasor.mkdir()
        glossary.write(str(dosya_ismi), "Json")

    def kindle(self) -> None:
        self.bSesleri_ekle = False
        glossary = self.glossary()
        klasor = BETIK_DY / "KubbealtiLugati_Kindle"
        dosya_ismi = klasor / "KubbealtiLugati"
        if not klasor.exists():
            klasor.mkdir()
        glossary.write(str(dosya_ismi), "Mobi", kindlegen_path="kindlegen")
        mobi_dosya_yolu = dosya_ismi / "OEBPS" / "content.mobi"
        if mobi_dosya_yolu.exists():
            mobi_dosya_yolu.replace(klasor / "KubbealtiLugati.mobi")

    def kobo(self) -> None:
        self.bSesleri_ekle = False
        glossary = self.glossary()
        klasor = BETIK_DY / "KubbealtiLugati_Kobo"
        dosya_ismi = klasor / "KubbealtiLugati.df"
        if not klasor.exists():
            klasor.mkdir()
        glossary.write(str(dosya_ismi), "Dictfile")
        subprocess.Popen(["dictgen-windows.exe", str(dosya_ismi),
                          "-o", str(klasor / "dicthtml-tr.zip")],
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

if __name__ == '__main__':
    parser = ArgumentParser(formatter_class=RawDescriptionHelpFormatter, description="""
    Kubbealtı Lugatı verilerini StarDict, Kobo dicthtml ve Kindle MOBI
    biçimlerine çeviren betik.

    Eğer girdilerin ses dosyaları elinizde varsa bunları betiğin yanında yer alan
    dosyalar/sesler konumuna kopyalayın. Ses dosyalarının uzantısı önemli değildir
    ancak isimlerinin her girdinin "id"sine denk gelmesi gerekmektedir.

    Kindle dönüşümü için "kindlegen.exe" çalıştırılabilir dosyasının PATH'de olması
    gerekmektedir.

    Kobo dönümüşü için "dictgen-windows.exe" çalıştırılabilir dosyasının PATH'de olması
    gerekmektedir.
    """)
    parser.add_argument("-d", "--veri-dosyasi",
                        default=(BETIK_DY / "dosyalar" / "Kubbealti2023Ed.json.tar.gz"), dest="json",
                        help="""Girdilerin yer aldığı JSON dosyasının konumu. tar.gz ile sıkıştırılmış
                        olabilir. JSON dosyasının yapısı için betiğin başında yer alan örneğe bakın.""")
    parser.add_argument("-s", "--ses-ekle", action="store_true", dest="bSes",
                        help="""Ses dosyaları sözlüğe eklensin.
                        Bu dosyalar sadece StarDict biçimine eklenmektedir.""")
    parser.add_argument("-b", "--bicim", choices=[1,2,3], default=1, type=int, dest="bicim",
                        help="""Sözlüğün dönüştürüleceği biçim. Geçerli seçenekler:
                        1 = StarDict, 2 = Kobo dicthtml, 3 = Kindle MOBI""")
    args = parser.parse_args()
    kubbealti = Kubbealti(args.json, args.bSes)
    match args.bicim:
        case 1:
            kubbealti.stardict()
        case 2:
            kubbealti.kobo()
        case 3:
            kubbealti.kindle()