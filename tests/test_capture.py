from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backuptool.engine import CaptureError, capture_html, google_captcha


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_captcha_is_rejected_before_pdf_printing(self):
        for url, body in (
            ('https://www.google.com/sorry/index', ''),
            ('https://www.google.co.uk/search', 'Our systems have detected unusual traffic'),
        ):
            with self.subTest(url=url), patch('backuptool.engine.print_page') as print_page:
                page = SimpleNamespace(url=url, title=AsyncMock(return_value=''), evaluate=AsyncMock(return_value=body))
                with self.assertRaisesRegex(CaptureError, 'Капча Google не пройдена') as error:
                    await capture_html(page, Path('unused.pdf'), 429)
                self.assertEqual((error.exception.url, error.exception.status), (url, 429))
                print_page.assert_not_called()

    def test_google_widget_or_mentions_on_other_domains_do_not_block_capture(self):
        self.assertFalse(google_captcha('https://biocolor.ru/natural-dyes.html', '', 'Google reCAPTCHA'))
        self.assertFalse(google_captcha('https://google.example.org/sorry/index', '', 'unusual traffic'))
