import io
import json
import unittest
from unittest.mock import patch

from test_cloud_archive import cloud


class RemoteAddressTests(unittest.TestCase):
    def validate(self, address, *, host="cdn.example", proxy=True, public_answer="8.8.8.8"):
        response = io.BytesIO(json.dumps({"Status": 0, "Answer": [
            {"type": 1, "data": public_answer},
        ]}).encode())
        with patch.object(cloud.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]), \
             patch.object(cloud.urllib.request, "getproxies", return_value={"https": "http://127.0.0.1:7897"} if proxy else {}), \
             patch.object(cloud.urllib.request, "proxy_bypass", return_value=False), \
             patch.object(cloud.urllib.request, "urlopen", return_value=response) as lookup:
            cloud._validate_public_http_url("https://" + host + "/video.mp4?signature=private")
            return lookup.call_args_list

    def test_proxy_fake_ip_validates_public_dns_without_sending_signed_url(self):
        calls = self.validate("198.18.0.86")
        self.assertEqual(1, len(calls))
        url = calls[0].args[0]
        self.assertIn("name=cdn.example", url)
        self.assertNotIn("signature", url)

    def test_public_address_needs_no_extra_lookup(self):
        self.assertEqual([], self.validate("8.8.8.8"))

    def test_private_loopback_and_metadata_addresses_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.validate(address)

    def test_fake_ip_without_proxy_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate("198.18.0.86", proxy=False)

    def test_literal_fake_ip_is_rejected_even_with_proxy(self):
        with self.assertRaises(ValueError):
            self.validate("198.18.0.86", host="198.18.0.86")

    def test_private_public_dns_answer_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate("198.18.0.86", public_answer="10.0.0.1")


if __name__ == "__main__":
    unittest.main()
