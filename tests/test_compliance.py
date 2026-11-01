import json

from app.compliance import PUBLIC_FIELDS, filter_people, scrub_contact_text, to_public_lead
from conftest import LEAKY_ROW, PRIVATE_STRINGS


def test_private_contact_details_are_filtered_out():
    lead = to_public_lead(LEAKY_ROW, provider="apollo", company_fallback="Example", company_domain="")

    assert set(lead) == set(PUBLIC_FIELDS)
    assert lead["name"] == "Alice Nakamoto"
    assert lead["title"] == "Head of BD"
    assert lead["company"] == "Example Labs"
    assert lead["profile_url"] == "https://www.linkedin.com/in/alice-nakamoto"
    assert lead["source"] == "treg.to (apollo)"

    dumped = json.dumps(lead)
    for private in PRIVATE_STRINGS:
        assert private not in dumped, private
    assert "@" not in dumped


def test_nested_provider_shape_is_read_and_still_filtered():
    # Shape of treg's aiark.people.search example, plus private data hidden in nested objects.
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "profile": {"first_name": "Jane", "last_name": "Example", "full_name": "Jane Example", "title": "Founder",
                    "phone": "+1 415 555 0199", "personal_email": "jane@gmail.com"},
        "link": {"linkedin": "https://www.linkedin.com/in/example", "twitter": "https://twitter.com/jane"},
        "location": {"country": "United States", "street": "742 Evergreen Terrace"},
        "company": {"id": "x", "name": "Example", "domain": "example.com"},
        "contact": {"email": "jane@example.com", "mobile": "+44 7700 900123"},
    }
    lead = to_public_lead(row, provider="aiark.people.search", company_fallback="Fallback", company_domain="")
    assert lead == {"name": "Jane Example", "title": "Founder", "company": "Example",
                    "profile_url": "https://www.linkedin.com/in/example", "source": "treg.to (aiark.people.search)"}
    dumped = json.dumps(lead)
    for private in ("555", "gmail", "twitter", "Evergreen", "jane@example.com", "7700"):
        assert private not in dumped


def test_position_list_shape_gives_title_and_company():
    # Shape of treg's harvestapi.linkedin.leads.search rows.
    row = {
        "id": "x", "linkedinUrl": "https://www.linkedin.com/in/sam-example", "firstName": "Sam", "lastName": "Example",
        "summary": "Call me on +1 415 555 0199", "pictureUrl": "https://media.licdn.com/p.jpg",
        "currentPositions": [
            {"title": "Advisor", "companyName": "Other Co", "current": False},
            {"title": "Head of Business Development", "companyName": "Chainlink Labs", "current": True,
             "description": "Reach me at sam@gmail.com"},
        ],
        "location": {"linkedinText": "Berlin"},
    }
    lead = to_public_lead(row, provider="harvestapi", company_fallback="Fallback", company_domain="")
    assert lead == {"name": "Sam Example", "title": "Head of Business Development", "company": "Chainlink Labs",
                    "profile_url": "https://www.linkedin.com/in/sam-example", "source": "treg.to (harvestapi)"}
    assert "555" not in json.dumps(lead) and "gmail" not in json.dumps(lead) and "Berlin" not in json.dumps(lead)


def test_nested_company_domain_gives_company_site_when_no_linkedin():
    row = {"profile": {"full_name": "Sam Ray", "title": "BD"}, "company": {"name": "Acme", "domain": "acme.io"}}
    lead = to_public_lead(row, provider="", company_fallback="Acme", company_domain="")
    assert lead["profile_url"] == "https://acme.io"


def test_scrub_contact_text_keeps_normal_titles():
    assert scrub_contact_text("VP Web3 Partnerships, L2 team") == "VP Web3 Partnerships, L2 team"
    assert scrub_contact_text("BD lead bob@x.io +49 30 1234567") == "BD lead"


def test_non_profile_urls_are_rejected_and_company_site_used():
    row = {"name": "Bob Lee", "title": "Recruiter", "linkedin_url": "https://www.linkedin.com/company/example",
           "url": "https://bob-personal.blog", "website": "https://careers.example.com/team"}
    lead = to_public_lead(row, provider="", company_fallback="Example", company_domain="example.com")
    assert lead["profile_url"] == "https://careers.example.com/team"
    assert lead["source"] == "treg.to"


def test_mailto_and_embedded_email_urls_are_rejected():
    row = {"name": "Carol", "linkedin_url": "mailto:carol@example.com", "url": "https://user@linkedin.com/in/carol"}
    lead = to_public_lead(row, provider="", company_fallback="Example", company_domain="")
    assert lead["profile_url"] == ""


def test_at_most_ten_people_and_deduplicated():
    rows = [{"name": f"Person {i}", "title": "BD"} for i in range(25)] + [{"name": "Person 0", "title": "BD"}]
    leads = filter_people(rows, provider="x", company="Acme", company_domain=None, limit=50)
    assert len(leads) == 10
    assert len({lead["name"] for lead in leads}) == 10


def test_people_at_look_alike_companies_are_dropped():
    rows = [
        {"name": "A One", "title": "BD", "company": "Nantong Link Metal Products Co.,Ltd"},
        {"name": "B Two", "title": "BD", "company": "LinkTop supply chain"},
        {"name": "C Three", "title": "BD", "company": "Chainlink Labs"},
        {"name": "D Four", "title": "BD", "company": "chainlink"},
        {"name": "E Five", "title": "BD", "company": "Chain Link Foundation"},
        {"name": "F Six", "title": "BD"},  # no company from the provider: kept with the requested one
        {"name": "G Seven", "title": "BD", "company": "Smartcontract", "website": "https://careers.chain.link"},
    ]
    leads = filter_people(rows, provider="x", company="Chainlink Labs", company_domain="chain.link")
    assert [lead["name"] for lead in leads] == ["C Three", "D Four", "E Five", "F Six", "G Seven"]


def test_role_matches():
    from app.compliance import role_matches
    assert role_matches("Senior Business Development Manager", "BD")
    assert role_matches("Head of BD, APAC", "business development")
    assert role_matches("Director, Strategic Partnerships", "partnerships")
    assert role_matches("Ecosystem Partner Lead", "partnerships")
    assert role_matches("Technical Recruiter", "recruiter")
    assert role_matches("Talent Acquisition Partner", "recruiter")
    assert role_matches("Head of Marketing", "marketing")
    assert role_matches("Anything", None)
    assert not role_matches("Software Engineer", "BD")
    assert not role_matches("", "partnerships")
    # Too-loose words seen in real Apollo results must not count.
    assert not role_matches("Ecosystem Media Lead", "business development")
    assert not role_matches("Ecosystem Marketing Manager", "business development")
    assert not role_matches("Blockchain Engineer, Integrations", "partnerships")
    assert not role_matches("Senior Talent Acquisition Partner", "partnerships")
    assert not role_matches("Global Human Resources Business Partner", "partnerships")
    assert role_matches("Ecosystem Growth Lead", "business development")
    assert role_matches("Director, Institutional Market Development", "BD")


def test_closest_titles_come_first():
    rows = [{"name": "A", "title": "Ecosystem Growth Lead"}, {"name": "B", "title": "Partnerships Manager"},
            {"name": "C", "title": "Business Development"}, {"name": "D", "title": "Head of BD"}]
    leads = filter_people(rows, provider="", company="Acme", company_domain=None, role="business development")
    assert [lead["name"] for lead in leads] == ["C", "D", "B", "A"]


def test_company_matches():
    from app.compliance import company_matches
    assert company_matches("Coinbase, Inc.", "Coinbase")
    assert company_matches("Uniswap Labs", "Uniswap")
    assert not company_matches("Coinbase Ventures Partners", "Uniswap")
    assert not company_matches("Link Supply Co", "Chainlink Labs")


def test_rows_without_name_are_dropped():
    leads = filter_people([{"title": "CTO", "email": "x@y.com"}, "junk", None], provider="", company="Acme",
                          company_domain=None)
    assert leads == []
