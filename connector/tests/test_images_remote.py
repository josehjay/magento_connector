"""
Image URLs built from Magento's media base URL (CDN / cloud storage aware)
instead of the hard-coded {magento_url}/media/ path.
"""

import unittest
from unittest.mock import MagicMock, patch

MEDIA = [{"types": ["image"], "file": "/a/b/book.jpg"}]


def _client_with(configs, store_code="default", cached=None):
    from connector.api.magento_client import MagentoClient

    client = MagentoClient.__new__(MagentoClient)
    client.base_url = "https://shop.example"
    client.store_code = store_code
    if isinstance(configs, Exception):
        client.get = MagicMock(side_effect=configs)
    else:
        client.get = MagicMock(return_value=configs)
    return client


class TestGetMediaBaseUrl(unittest.TestCase):

    @patch("connector.api.magento_client.frappe")
    def test_prefers_matching_store_and_secure_url(self, m):
        m.cache.return_value.get_value.return_value = None
        client = _client_with([
            {"code": "default", "base_media_url": "http://shop.example/media/",
             "secure_base_media_url": "https://shop.example/media/"},
            {"code": "kids", "base_media_url": "http://cdn.example/m",
             "secure_base_media_url": "https://cdn.example/m"},
        ], store_code="kids")

        self.assertEqual(client.get_media_base_url(), "https://cdn.example/m/")
        client.get.assert_called_once_with("/store/storeConfigs")
        set_calls = m.cache.return_value.set_value.call_args_list
        self.assertTrue(set_calls)
        self.assertEqual(set_calls[0].kwargs["expires_in_sec"], 6 * 60 * 60)

    @patch("connector.api.magento_client.frappe")
    def test_falls_back_to_first_entry_and_plain_media_url(self, m):
        m.cache.return_value.get_value.return_value = None
        client = _client_with([{"code": "other", "base_media_url": "https://cdn.example/media/"}])
        self.assertEqual(client.get_media_base_url(), "https://cdn.example/media/")

    @patch("connector.api.magento_client.frappe")
    def test_uses_cache_without_api_call(self, m):
        m.cache.return_value.get_value.return_value = "https://cdn.example/media/"
        client = _client_with([])
        self.assertEqual(client.get_media_base_url(), "https://cdn.example/media/")
        client.get.assert_not_called()

    @patch("connector.api.magento_client.frappe")
    def test_api_failure_returns_none(self, m):
        from connector.api.magento_client import MagentoAPIError

        m.cache.return_value.get_value.return_value = None
        client = _client_with(MagentoAPIError("forbidden", 403))
        self.assertIsNone(client.get_media_base_url())


class TestExtractBaseImageUrl(unittest.TestCase):

    @patch("connector.sync.image_sync.frappe")
    def test_uses_media_base_url(self, m):
        from connector.sync.image_sync import _extract_base_image_url

        self.assertEqual(
            _extract_base_image_url(MEDIA, "https://shop.example", "https://cdn.example/media/"),
            "https://cdn.example/media/catalog/product/a/b/book.jpg",
        )
        # No trailing slash on the base is handled too.
        self.assertEqual(
            _extract_base_image_url(MEDIA, "https://shop.example", "https://cdn.example/media"),
            "https://cdn.example/media/catalog/product/a/b/book.jpg",
        )

    @patch("connector.sync.image_sync.frappe")
    def test_legacy_fallback_when_no_media_base(self, m):
        from connector.sync.image_sync import _extract_base_image_url

        m.cache.return_value.get_value.return_value = None
        self.assertEqual(
            _extract_base_image_url(MEDIA, "https://shop.example/"),
            "https://shop.example/media/catalog/product/a/b/book.jpg",
        )

    @patch("connector.sync.image_sync.frappe")
    def test_two_arg_callers_pick_up_cached_media_base(self, m):
        """product_sync / magento_settings still call with two args."""
        from connector.sync.image_sync import _extract_base_image_url

        m.cache.return_value.get_value.return_value = "https://cdn.example/media/"
        self.assertEqual(
            _extract_base_image_url(MEDIA, "https://shop.example"),
            "https://cdn.example/media/catalog/product/a/b/book.jpg",
        )

    @patch("connector.sync.image_sync.frappe")
    def test_first_entry_used_when_no_base_type(self, m):
        from connector.sync.image_sync import _extract_base_image_url

        media = [{"types": [], "file": "/x/y.png"}]
        self.assertEqual(
            _extract_base_image_url(media, "https://shop.example", "https://cdn.example/media/"),
            "https://cdn.example/media/catalog/product/x/y.png",
        )


class TestSyncImagesUsesMediaBase(unittest.TestCase):

    @patch("connector.sync.image_sync.create_log")
    @patch("connector.sync.image_sync._is_sync_enabled", return_value=True)
    @patch("connector.sync.image_sync._get_item_image_field", return_value="image")
    @patch("connector.sync.image_sync.frappe")
    def test_sync_images_stores_cdn_url(self, m, _field, _en, _log):
        from connector.sync.image_sync import sync_images

        m.db.get_single_value.return_value = "https://shop.example"
        m.get_all.return_value = [{"item_code": "A", "magento_sku": "A"}]
        m.db.get_value.return_value = None
        client = MagicMock()
        client.get_product_media.return_value = MEDIA
        client.get_media_base_url.return_value = "https://cdn.example/media/"
        with patch("connector.sync.image_sync.MagentoClient", return_value=client):
            summary = sync_images()

        self.assertEqual(summary["updated"], 1)
        m.db.set_value.assert_called_once_with(
            "Item", "A", "image", "https://cdn.example/media/catalog/product/a/b/book.jpg",
            update_modified=False,
        )
        client.get_media_base_url.assert_called_once()

    @patch("connector.sync.image_sync.create_log")
    @patch("connector.sync.image_sync._is_sync_enabled", return_value=True)
    @patch("connector.sync.image_sync._get_item_image_field", return_value="image")
    @patch("connector.sync.image_sync.frappe")
    def test_media_base_failure_falls_back_to_magento_url(self, m, _field, _en, _log):
        from connector.sync.image_sync import sync_images

        m.db.get_single_value.return_value = "https://shop.example"
        m.get_all.return_value = [{"item_code": "A", "magento_sku": "A"}]
        m.db.get_value.return_value = None
        m.cache.return_value.get_value.return_value = None
        client = MagicMock()
        client.get_product_media.return_value = MEDIA
        client.get_media_base_url.side_effect = RuntimeError("down")
        with patch("connector.sync.image_sync.MagentoClient", return_value=client):
            sync_images()

        self.assertEqual(
            m.db.set_value.call_args.args[3],
            "https://shop.example/media/catalog/product/a/b/book.jpg",
        )


class TestSafeImageUrl(unittest.TestCase):

    def test_media_host_accepted_foreign_host_rejected(self):
        from connector.sync.image_sync import _is_safe_image_url

        shop = "https://shop.example"
        cdn = "https://cdn.example/media/"
        self.assertTrue(_is_safe_image_url("https://shop.example/media/x.jpg", shop))
        self.assertFalse(_is_safe_image_url("https://cdn.example/media/x.jpg", shop))
        self.assertTrue(_is_safe_image_url("https://cdn.example/media/x.jpg", shop, cdn))
        self.assertFalse(_is_safe_image_url("https://evil.example/x.jpg", shop, cdn))
        self.assertFalse(_is_safe_image_url("http://cdn.example/media/x.jpg", shop, cdn))

    @patch("connector.sync.image_sync.verify_incoming_signed_request")
    @patch("connector.sync.image_sync.require_push_endpoint_access")
    @patch("connector.sync.image_sync._is_sync_enabled", return_value=True)
    @patch("connector.sync.image_sync._get_item_image_field", return_value="image")
    @patch("connector.sync.image_sync.frappe")
    def test_receive_image_update_accepts_cdn_url(self, m, _field, _en, _acc, _sig):
        from connector.sync.image_sync import receive_image_update

        m.db.get_single_value.return_value = "https://shop.example"
        m.cache.return_value.get_value.return_value = None
        m.db.get_value.side_effect = ["A", None]
        client = MagicMock()
        client.get_media_base_url.return_value = "https://cdn.example/media/"
        url = "https://cdn.example/media/catalog/product/a/b/book.jpg"
        with patch("connector.sync.image_sync.MagentoClient", return_value=client):
            result = receive_image_update("A", url)

        self.assertEqual(result, {"ok": True, "item_code": "A"})
        m.db.set_value.assert_called_once_with("Item", "A", "image", url, update_modified=False)

    @patch("connector.sync.image_sync.verify_incoming_signed_request")
    @patch("connector.sync.image_sync.require_push_endpoint_access")
    @patch("connector.sync.image_sync._is_sync_enabled", return_value=True)
    @patch("connector.sync.image_sync.frappe")
    def test_same_host_push_needs_no_api_call(self, m, _en, _acc, _sig):
        from connector.sync.image_sync import receive_image_update

        m.db.get_single_value.return_value = "https://shop.example"
        m.cache.return_value.get_value.return_value = None
        with patch("connector.sync.image_sync.MagentoClient") as client_cls, \
             patch("connector.sync.image_sync._get_item_image_field", return_value="image"):
            m.db.get_value.side_effect = ["A", None]
            receive_image_update("A", "https://shop.example/media/catalog/product/x.jpg")
        client_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
