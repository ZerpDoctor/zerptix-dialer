import unittest

from app import ivr


class Night20261011(unittest.TestCase):
    # A phone number read out in a voicemail greeting is not a menu option (G S Kusich pressed 7244, Flood Restoration
    # Services pressed 8716 -- 2026-10-11).
    KUSICH = ("Hi. You've reached the office of GS Cusick Construction. We are currently unable to take your call. Please leave "
              "your name, number, and a brief message, and we'll return your call as soon as possible. If this is an emergency, "
              "please dial seven two four four five six two seven nine six. Or seven two four four five six eight seven six three.")
    FLOOD = ("You for choosing Flow Restoration Services. We are with another customer at the moment. Please leave your name "
             "and number. We will return your call promptly. For further assistance, please dial eight seven one six three six four.")

    def test_a_phone_number_read_out_is_never_pressed(self):
        for t in (self.KUSICH, self.FLOOD):
            self.assertIsNone(ivr.quick_digit(t))
            self.assertEqual(ivr.parse_options(t), [])

    def test_short_spoken_extensions_still_work(self):
        self.assertEqual(ivr.quick_digit("Please dial one one eight for the office.")[0], "118")
        self.assertEqual(ivr.quick_digit("For service dial two hundred.")[0], "200")


if __name__ == "__main__":
    unittest.main()
