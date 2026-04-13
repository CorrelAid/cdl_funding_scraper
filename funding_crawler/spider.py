import hashlib
from scrapy import Request, Spider
from scrapy.exceptions import CloseSpider
from datetime import datetime
from funding_crawler.helpers import compute_checksum, gen_license
from funding_crawler.models import FundingProgramSchema
from w3lib.url import canonicalize_url
from pydantic import ValidationError

# Mapping of internal codes to display names, extracted from the overview page sidebar filters.
# Some detail pages serve these codes instead of the human-readable labels due to a website bug.
code_to_label = {
    # funding_area (Förderbereich)
    "arbeit": "Arbeit",
    "aus_weiterbildung": "Aus- & Weiterbildung",
    "aussenwirtschaft": "Außenwirtschaft",
    "beratung": "Beratung",
    "corona": "Corona-Hilfe",
    "digitalisierung": "Digitalisierung",
    "energieeffizienz_erneuerbare_energien": "Energieeffizienz & Erneuerbare Energien",
    "existenzgruendung_festigung": "Existenzgründung & -festigung",
    "forschung_innovation_themenoffen": "Forschung & Innovation (themenoffen)",
    "forschung_innovation_themenspezifisch": "Forschung & Innovation (themenspezifisch)",
    "frauenfoerderung": "Frauenförderung",
    "gesundheit_soziales": "Gesundheit & Soziales",
    "infrastruktur": "Infrastruktur",
    "kultur_medien_sport": "Kultur, Medien & Sport",
    "landwirtschaft_laendliche_entwicklung": "Landwirtschaft & Ländliche Entwicklung",
    "messen_ausstellungen": "Messen & Ausstellungen",
    "mobilitaet": "Mobilität",
    "regionalfoerderung": "Regionalförderung",
    "smart_cities_regionen": "Smart Cities & Regionen",
    "staedtebau_stadterneuerung": "Städtebau & Stadterneuerung",
    "umwelt_naturschutz": "Umwelt- & Naturschutz",
    "unternehmensfinanzierung": "Unternehmensfinanzierung",
    "wohnungsbau_modernisierung": "Wohnungsbau & Modernisierung",
    # funding_type (Förderart)
    "beteiligung": "Beteiligung",
    "buergschaft": "Bürgschaft",
    "darlehen": "Darlehen",
    "garantie": "Garantie",
    "sonstige": "Sonstige",
    "zuschuss": "Zuschuss",
    # funding_location (Fördergebiet)
    "_bundesweit": "bundesweit",
    "baden_wuerttemberg": "Baden-Württemberg",
    "bayern": "Bayern",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "bremen": "Bremen",
    "de_ni": "Niedersachsen",
    "de_st": "Sachsen-Anhalt",
    "hamburg": "Hamburg",
    "hessen": "Hessen",
    "mecklenburg_vorpommern": "Mecklenburg-Vorpommern",
    "nordrhein_westfalen": "Nordrhein-Westfalen",
    "rheinland_pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland",
    "sachsen": "Sachsen",
    "schleswig_holstein": "Schleswig-Holstein",
    "thueringen": "Thüringen",
    # eligible_applicants (Förderberechtigte)
    "bildungseinrichtung": "Bildungseinrichtung",
    "existenzgruenderin": "Existenzgründer/in",
    "forschungseinrichtung": "Forschungseinrichtung",
    "hochschule": "Hochschule",
    "kommune": "Kommune",
    "oeffentliche_einrichtung": "Öffentliche Einrichtung",
    "privatperson": "Privatperson",
    "unternehmen": "Unternehmen",
    "verband_vereinigung": "Verband/Vereinigung",
}

# Known display labels, used to reconstruct labels that contain the list separator ", "
# (e.g. "Kultur, Medien & Sport" gets split incorrectly by a naive split on ", ").
known_labels = set(code_to_label.values())

translate_map = {
    "Kurzzusammenfassung": "description",
    "Zusatzinfos": "more_info",
    "Rechtsgrundlage": "legal_basis",
    "Ansprechpunkt": "contact_info",
    "Weiterführende Links": "further_links",
    "WeiterfuehrendeLinks": "further_links",  # ASCII variant
    "Förderart": "funding_type",
    "Foerderart": "funding_type",  # ASCII variant
    "Förderbereich": "funding_area",
    "Foerderbereich": "funding_area",  # ASCII variant
    "Fördergebiet": "funding_location",
    "Foerdergebiet": "funding_location",  # ASCII variant
    "Förderberechtigte": "eligible_applicants",
    "Foerderberechtigte": "eligible_applicants",  # ASCII variant
    "Fördergeber": "funding_body",
    "Foerdergeber": "funding_body",  # ASCII variant
}


class FundingSpider(Spider):
    """
    A Scrapy spider for extracting funding program details from a website.
    The spider crawls through pages of funding programs, extracts program details,
    and navigates through pagination.
    """

    name = "funding"

    def __init__(self, *args, **kwargs):
        super(FundingSpider, self).__init__(*args, **kwargs)
        self.total_cards_found = 0
        self.unique_urls = {}  # URL -> (page_number, page_url) mapping
        self.page_count = 0

    def parse(self, response):
        """
        Parse the response from the main page listing funding programs.

        Extracts URLs for individual funding program details and follows them
        for further parsing. Also handles pagination to fetch additional program listings.

        Args:
            response (Response): The HTTP response object for the current page.

        Yields:
            Request: Requests for detailed program pages and the next pagination page.
        """
        urls = response.css(
            'div.card--fundingprogram > p.card--title > a::attr("href")'
        ).extract()

        cards = response.css("div.card--fundingprogram")
        self.total_cards_found += len(cards)
        self.page_count += 1

        self.logger.debug(
            f"Processing page {self.page_count}: {response.url} (found {len(cards)} cards)"
        )

        if not cards:
            self.logger.warning(
                f"No funding program cards found on page: {response.url}"
            )
        elif len(cards) < 10 or len(cards) > 10:
            self.logger.warning(
                f"Found {len(cards)} funding program cards on page: {response.url} (expected 10)"
            )
        else:
            pass

        if self.page_count % 10 == 0:
            self.logger.info(f"Total cards found so far: {self.total_cards_found}")

        for i, card in enumerate(cards):
            title_link = card.css('p.card--title > a::attr("href")').get()
            title_text = card.css("p.card--title > a::text").get()

            if not title_link:
                self.logger.warning(f"Card {i+1} on {response.url} missing title link")
            if not title_text:
                self.logger.warning(f"Card {i+1} on {response.url} missing title text")

        for url in urls:
            full_url = response.urljoin(url)
            normalized = canonicalize_url(full_url)

            if normalized in self.unique_urls:
                original_page_num, original_page_url = self.unique_urls[normalized]
                self.logger.warning(
                    f"Skipping duplicate URL: {normalized}. Duplicate found on page {self.page_count}: {response.url}. Originally found on page {original_page_num}: {original_page_url}"
                )
                continue

            self.unique_urls[normalized] = (self.page_count, response.url)
            yield Request(url=normalized, callback=self.parse_details)

        if self.page_count % 10 == 0:
            self.logger.info(f"Total unique URLs found so far: {len(self.unique_urls)}")

        next_page = response.css('a.forward.button::attr("href")').get()

        if next_page is not None and next_page != "":
            yield response.follow(next_page, self.parse)

    def parse_details(self, response):
        """
        Parse the response from a funding program detail page.

        Extracts details such as title, description, funding information, contact details,
        and additional links. Computes unique identifiers and a checksum for the data.

        Args:
            response (Response): The HTTP response object for the detail page.

        Yields:
            dict: A dictionary containing the extracted program details.
        """
        dct = {}

        dct["title"] = "".join(
            response.xpath("//h1[@class='title']//text()").getall()
        ).strip()
        dct["title"] = dct["title"] if dct["title"] else None

        if not dct["title"]:
            self.logger.warning(f"No title found on page: {response.url}")
            raise ValueError(f"No title found on page: {response.url}")

        tab_names = response.xpath(
            "/html/body/main/div[2]/div/div[1]/h2/span//text()"
        ).getall()

        if tab_names:
            # try format with article tags first
            article_nodes = response.xpath("//div[@class='content']//article")

            # if no articles found, try older format where content is in rich--text div
            if not article_nodes:
                article_nodes = response.xpath("//div[@class='rich--text']")

            for i, article in enumerate(article_nodes):
                content = article.get()
                try:
                    key = translate_map[tab_names[i].strip()]
                except KeyError:
                    self.logger.error(
                        f"Unknown tab name: '{tab_names[i].strip()}' on page: {response.url}"
                    )
                    raise CloseSpider(
                        f"Unknown tab name: '{tab_names[i].strip()}' on page: {response.url}"
                    )

                dct[key] = content

            # Log warning if description is missing
            if "description" not in dct:
                self.logger.warning(
                    f"No 'Kurzzusammenfassung' (description) tab found on page: {response.url}. Found tabs: {tab_names}"
                )
                dct["description"] = None
        else:
            content_node = response.xpath(
                "//main/div[@class='jumbotron']/following-sibling::div/div[@class='content']"
            ).get()

            if not content_node:
                self.logger.warning(f"No description found on page: {response.url}")
                dct["description"] = None
            else:
                dct["description"] = content_node

        dt_elements = response.xpath("//dt")
        dd_elements = response.xpath("//dd")

        for dt, dd in zip(dt_elements, dd_elements):
            key = dt.xpath("text()").get()
            if key:
                key = translate_map.get(key.strip().replace(":", ""))
                if not key:
                    self.logger.warning("Field not in translate map: %s", key)
                    continue
            else:
                self.logger.warning("Field couldnt be extracted: %s %s", dt, dd)
                continue

            if key in [
                "funding_type",
                "funding_area",
                "funding_location",
                "eligible_applicants",
            ]:
                lst_str = dd.xpath("text()").get()
                parts = lst_str.strip().split(", ") if lst_str else []
                # Merge consecutive parts that form a known label containing ", "
                # (e.g. "Kultur, Medien & Sport" would otherwise be split in two).
                merged = []
                i = 0
                while i < len(parts):
                    if i + 1 < len(parts):
                        combined = parts[i] + ", " + parts[i + 1]
                        if combined in known_labels:
                            merged.append(combined)
                            i += 2
                            continue
                    merged.append(parts[i])
                    i += 1
                lst = [code_to_label.get(v, v) for v in merged]
                dct[key] = lst if lst else None

            elif key == "funding_body":
                # Try both umlaut and ASCII versions of the title attribute
                str = dd.xpath(
                    "p[@class='card--title']/a[@title='Öffnet die Einzelsicht' or @title='OeffnetEinzelsicht']/span[@class='link--label']/text()"
                ).get()
                dct[key] = str.strip() if str else None

            elif key == "further_links":
                links = []
                for link in dd.xpath(".//a[@href]"):
                    link_url = link.xpath("@href").get()
                    if not link_url.startswith("http"):
                        link_url = "https://www.foerderdatenbank.de/" + link_url
                    links.append(link_url)

                dct[key] = links if links else None

            elif key == "contact_info":
                dct["contact_info_institution"] = (
                    " ".join(
                        dd.xpath(
                            ".//a[@title='Öffnet die Einzelsicht' or @title='OeffnetEinzelsicht']/span[@class='link--label']//text()"
                        ).getall()
                    ).strip()
                    or None
                )

                dct["contact_info_street"] = (
                    dd.xpath(".//p[@class='adr']/text()").get() or ""
                ).strip() or None

                dct["contact_info_city"] = (
                    dd.xpath(".//p[@class='locality']/text()").get() or ""
                ).strip() or None

                dct["contact_info_fax"] = (
                    dd.xpath(".//p[@class='fax']/text()").re_first(r"Fax:\s*(.*)")
                    or None
                )

                dct["contact_info_phone"] = (
                    dd.xpath(".//p[@class='tel']/text()").re_first(r"Tel:\s*(.*)")
                    or None
                )

                dct["contact_info_email"] = (
                    dd.xpath(".//p[@class='email']/a[@href]")
                    .xpath("@href")
                    .re_first(r"mailto:(.*)")
                    or None
                )

                dct["contact_info_website"] = (
                    dd.xpath(".//p[@class='website']/a[@href]").xpath("@href").get()
                    or None
                )

            else:
                value = dd.xpath("text()").get()
                dct[key] = value.strip() if value else None

        url_parts = response.url.partition("Foerderprogramm/")

        if url_parts[1] == "":
            url_parts = response.url.partition(
                "Archiv/"
            )  # e.g. 'https://www.foerderdatenbank.de/FDB/Content/DE/Archiv/innovativer-schiffbau-sichert-arbeitsplaetze.html'

        foerderprogramm_url_id = (
            url_parts[2].replace("/", "-").replace(".html", "").lower()
        )
        foerderprogramm_hash_id = hashlib.md5(
            foerderprogramm_url_id.encode()
        ).hexdigest()

        dct["url"] = response.url
        dct["id_hash"] = foerderprogramm_hash_id
        dct["id_url"] = foerderprogramm_url_id

        ignore_fields = ["url", "id_hash", "id_url"]
        watch_fields = [x for x in list(dct.keys()) if x not in ignore_fields]

        dct["checksum"] = compute_checksum(dct, watch_fields)

        date = datetime.today()
        dct["license_info"] = gen_license(dct["title"], date, dct["url"])

        # Validate the item with Pydantic schema (applies after_validators)
        try:
            FundingProgramSchema(**dct)
        except ValidationError as e:
            self.logger.error(f"Validation error for program at {response.url}: {e}")
            raise CloseSpider(f"Validation error for program at {response.url}: {e}")

        yield dct
