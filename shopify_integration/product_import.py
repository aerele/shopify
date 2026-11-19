# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Product Sync adapter for the shared Ecommerce Core page (create-only, multi-store)."""

import base64
import json

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from ecommerce_core.product_import.adapter import ProductImportAdapter
from frappe import _
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import MODULE_NAME
from shopify_integration.shopify.page.shopify_import_products.shopify_import_products import (
	_fetch_products_from_shopify,
	_gid_to_id,
	get_shopify_product_count,
)
from shopify_integration.shopify.product import ShopifyProduct
from shopify_integration.shopify.utils import get_enabled_accounts

_PRODUCT_VARIANTS_QUERY = """
query product($id: ID!) {
	product(id: $id) {
		variants(first: 100) {
			edges {
				node {
					id
				}
			}
		}
	}
}
"""


def _pack_id(account: str, product_id) -> str:
	# Stores can reuse the same numeric product id, so the page id carries the store.
	return f"{account}:{product_id}"


def _unpack_id(value: str) -> tuple[str, str]:
	account, _, product_id = str(value).partition(":")
	return account, product_id


def _encode_cursor(account_index: int, shop_cursor: str | None) -> str:
	payload = base64.b64encode(
		json.dumps({"i": account_index, "c": shop_cursor}, separators=(",", ":")).encode()
	).decode()
	return "s:" + payload


def _decode_cursor(cursor: str | None) -> tuple[int, str | None]:
	if not cursor or not str(cursor).startswith("s:"):
		return 0, None
	try:
		data = json.loads(base64.b64decode(str(cursor)[2:]))
		return max(int(data.get("i") or 0), 0), data.get("c")
	except Exception:
		return 0, None


class ShopifyProductImportAdapter(ProductImportAdapter):
	def is_enabled(self) -> bool:
		return bool(get_enabled_accounts())

	def get_products(self, limit: int, cursor: str | None = None, search: str | None = None) -> dict:
		"""Walk the enabled stores in order; the cursor carries the store index and its shop cursor."""
		accounts = get_enabled_accounts()
		index, shop_cursor = _decode_cursor(cursor)
		products: list[dict] = []
		next_cursor = None
		while index < len(accounts) and len(products) < limit:
			account = accounts[index]
			page = _fetch_products_from_shopify(
				from_=shop_cursor, limit=limit - len(products), query=search, shopify_account=account
			)
			for product in page.get("products") or []:
				row = dict(product)
				row["id"] = _pack_id(account, product.get("id"))
				row["shopify_account"] = account
				products.append(row)
			if page.get("next_cursor"):
				next_cursor = _encode_cursor(index, page["next_cursor"])
				break
			index += 1
			shop_cursor = None
			next_cursor = _encode_cursor(index, None) if index < len(accounts) else None
		return {"products": products, "next_cursor": next_cursor, "total": None}

	def get_provider_count(self) -> int:
		return sum(
			get_shopify_product_count(shopify_account=account) or 0 for account in get_enabled_accounts()
		)

	def sync_product(self, product_id: str) -> dict:
		account, product_id = _unpack_id(product_id)
		if not account or not product_id:
			frappe.throw(_("Product id is missing its store prefix."))
		was_synced = ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=product_id,
			filters={"shopify_account": account},
		)
		if was_synced:
			# No update path exists yet; report skipped instead of a false "updated".
			return {"action": "skipped"}
		for variant_id in self._variant_ids(product_id, shopify_account=account) or [None]:
			ShopifyProduct(product_id, variant_id=variant_id, shopify_account=account).sync_product()
		return {"action": "created"}

	@temp_shopify_session
	def _variant_ids(self, product_id: str, shopify_account=None):
		response = json.loads(
			GraphQL().execute(_PRODUCT_VARIANTS_QUERY, {"id": f"gid://shopify/Product/{product_id}"})
		)
		product = response.get("data", {}).get("product")
		if not product:
			frappe.throw(f"Shopify product {product_id} not found")
		edges = (product.get("variants") or {}).get("edges", [])
		return [_gid_to_id((edge.get("node") or {}).get("id")) for edge in edges]
