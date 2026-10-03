"""The sent-mail -> email_track matcher. A wrong match would hold the wrong company for 90 days (or miss one
we emailed), so every rule here is pinned."""
from __future__ import annotations

import unittest

from app import email_sync as es


def rows():
    return [
        {"row": 10, "domain": "https://www.AcmeRestoration.com/contact", "source": "manual_import_2026", "email_track": ""},
        {"row": 11, "domain": "http://bioclean-nc.com/", "source": "", "email_track": ""},          # two branches share
        {"row": 12, "domain": "https://www.bioclean-nc.com/", "source": "", "email_track": ""},      # one website
        {"row": 13, "domain": "claycorp.com", "source": "clay_import_2026_pass2", "email_track": ""},
        {"row": 14, "domain": "oldmail.com", "source": "manual_import_2026", "email_track": "2026-09-30"},
        {"row": 15, "domain": "tagged.com", "source": "", "email_track": "2026-Q3"},
        {"row": 16, "domain": "", "source": "", "email_track": ""},
    ]


class Domains(unittest.TestCase):
    def test_normalising_a_website(self):
        self.assertEqual(es.norm_domain("https://www.AcmeRestoration.com/contact?x=1"), "acmerestoration.com")
        self.assertEqual(es.norm_domain("http://bioclean-nc.com:8080/"), "bioclean-nc.com")
        self.assertEqual(es.norm_domain("jane@Foo.com"), "foo.com")
        self.assertEqual(es.norm_domain(""), "")
        self.assertEqual(es.norm_domain("localhost"), "")

    def test_recipients_skip_mailbox_providers_and_the_sender(self):
        d = es.recipient_domains(["Jane <jane@acmerestoration.com>, bob@gmail.com", "me@mycompany.com, ops@mycompany.com"],
                                 own_addresses={"me@mycompany.com"})
        self.assertEqual(d, {"acmerestoration.com"})

    def test_a_subdomain_matches_its_parent_but_not_a_lookalike(self):
        idx = es.build_index(rows())
        self.assertEqual(es.match_rows("acmerestoration.com", idx), [10])
        self.assertEqual(es.match_rows("mail.acmerestoration.com", idx), [10])
        self.assertEqual(es.match_rows("notacmerestoration.com", idx), [])
        self.assertEqual(es.match_rows("acmerestoration.net", idx), [])
        self.assertEqual(es.match_rows("com", idx), [])


class Planning(unittest.TestCase):
    def send(self, when, *domains):
        return {"date": when, "domains": set(domains)}

    def test_latest_send_wins_and_every_branch_sharing_a_website_is_marked(self):
        p = es.plan([self.send("2026-09-05", "bioclean-nc.com"), self.send("2026-09-20", "bioclean-nc.com")], rows())
        self.assertEqual(sorted(p.writes), [(11, "2026-09-20"), (12, "2026-09-20")])
        self.assertEqual(p.multi_row_domains, {"bioclean-nc.com": [11, 12]})

    def test_clay_table_companies_are_held_back_unless_asked(self):
        s = [self.send("2026-09-26", "claycorp.com")]
        p = es.plan(s, rows())
        self.assertEqual((p.writes, p.clay_held_back), ([], [(13, "2026-09-26")]))
        p2 = es.plan(s, rows(), include_clay=True)
        self.assertEqual(p2.writes, [(13, "2026-09-26")])

    def test_an_existing_newer_or_equal_date_is_not_touched_and_an_older_one_is_replaced(self):
        p = es.plan([self.send("2026-09-30", "oldmail.com")], rows())
        self.assertEqual((p.writes, p.already_ok), ([], 1))
        p = es.plan([self.send("2026-10-02", "oldmail.com")], rows())
        self.assertEqual(p.writes, [(14, "2026-10-02")])

    def test_a_humans_note_in_email_track_is_never_overwritten(self):
        p = es.plan([self.send("2026-10-02", "tagged.com")], rows())
        self.assertEqual((p.writes, p.kept_text), ([], [15]))

    def test_unknown_domains_are_listed_not_guessed(self):
        p = es.plan([self.send("2026-09-10", "nobody-we-know.com"), self.send("2026-09-12", "nobody-we-know.com")], rows())
        self.assertEqual(p.writes, [])
        self.assertEqual(p.unmatched, {"nobody-we-know.com": (2, "2026-09-12")})

    def test_sends_with_no_company_recipient_are_ignored(self):
        p = es.plan([{"date": "2026-09-10", "domains": set()}], rows())
        self.assertEqual((p.sends_used, p.writes, p.unmatched), (0, [], {}))


if __name__ == "__main__":
    unittest.main()


class Aliases(unittest.TestCase):
    def test_an_approved_alias_is_used_only_when_there_is_no_direct_match(self):
        rs = [{"row": 20, "domain": "http://different-site.com", "source": "", "email_track": ""},
              {"row": 21, "domain": "acme.com", "source": "", "email_track": ""}]
        sends = [{"date": "2026-09-10", "domains": {"acme-mail.com", "acme.com"}}]
        p = es.plan(sends, rs, aliases={"acme-mail.com": [20], "acme.com": [20]})
        self.assertEqual(sorted(p.writes), [(20, "2026-09-10"), (21, "2026-09-10")])     # direct match for acme.com still goes to row 21
        p2 = es.plan([{"date": "2026-09-10", "domains": {"acme-mail.com"}}], rs)
        self.assertEqual((p2.writes, list(p2.unmatched)), ([], ["acme-mail.com"]))      # no alias, no guess
