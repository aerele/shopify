"""Product Sync adapter for the shared Ecommerce Core page."""

from __future__ import annotations

import frappe
from ecommerce_core.product_import.adapter import ProductImportAdapter
from frappe import _

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import MODULE_NAME
from shopify_integration.shopify.page.shopify_import_products.shopify_import_products import (
	_build_product_search_query,
	_fetch_products_from_shopify,
	_fetch_variant_ids,
	get_shopify_product_count,
)
from shopify_integration.shopify.product import ShopifyProduct
from shopify_integration.shopify.utils import get_default_account, get_enabled_accounts


class ShopifyProductImportAdapter(ProductImportAdapter):
	"""Contract implementation for ``ecommerce_core.product_import``."""

	account_field = "shopify_account"

	def is_enabled(self) -> bool:
		return bool(get_enabled_accounts())

	def get_accounts(self) -> list[dict]:
		return [{"name": name, "label": name} for name in get_enabled_accounts()]

	@staticmethod
	def _account(account: str | None = None) -> str:
		"""Store the shared page works on: the one it picked, else the only enabled account.

		``get_default_account`` throws when several are enabled rather than guessing.
		"""
		return account or get_default_account().name

	def get_products(
		self, limit: int, cursor: str | None = None, search: str | None = None, account: str | None = None
	) -> dict:
		# The opaque cursor is the connector's own "n:<endCursor>" token.
		page = _fetch_products_from_shopify(
			from_=cursor,
			limit=limit,
			query=_build_product_search_query(search),
			shopify_account=self._account(account),
		)
		return {
			"products": [
				{
					"id": product["id"],
					"name": product["title"],
					"sku": next((v["sku"] for v in product["variants"] if v.get("sku")), None),
					"variants": product["variants"],
					"updated_at": product.get("updated_at"),
				}
				for product in page["products"]
			],
			"total": None,
			"next_cursor": page["next_cursor"],
		}

	def get_provider_count(self, account: str | None = None) -> int:
		return int(get_shopify_product_count(shopify_account=self._account(account)) or 0)

	def sync_product(self, product_id: str, account: str | None = None) -> dict:
		account = self._account(account)
		product_id = str(product_id)
		variant_ids = {str(v) for v in self._variant_ids(product_id, shopify_account=account)}

		mapped = frappe.get_all(
			"Ecommerce Item",
			filters={
				"integration": MODULE_NAME,
				"shopify_account": account,
				"integration_item_code": product_id,
			},
			fields=["variant_id", "has_variants"],
		)
		if not mapped:
			ShopifyProduct(product_id, shopify_account=account).sync_product()
			return {"action": "created"}

		has_template = any(row.has_variants for row in mapped)
		mapped_variants = {str(row.variant_id) for row in mapped if not row.has_variants and row.variant_id}
		# a product without options is one item mapped by its only variant, with no template row
		if variant_ids <= mapped_variants and (has_template or len(variant_ids) <= 1):
			# No update path exists yet; report skipped instead of a false "updated".
			return {"action": "skipped"}

		ShopifyProduct(product_id, shopify_account=account).sync_missing_variants()
		return {"action": "updated"}

	@temp_shopify_session
	def _variant_ids(self, product_id: str, shopify_account: str | None = None) -> list[str]:
		"""All variant ids of the product (paginated; raises on GraphQL errors)."""
		variant_ids = _fetch_variant_ids(product_id)
		if variant_ids is None:
			frappe.throw(_("Shopify product {0} not found").format(product_id))
		return variant_ids
