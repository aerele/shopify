import json
import re
from time import process_time

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from ecommerce_core.product_import.exceptions import TransientProviderError
from frappe import _
from frappe.exceptions import UniqueValidationError
from frappe.utils import cint
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import ACCOUNT_DOCTYPE, MODULE_NAME
from shopify_integration.shopify.product import ShopifyProduct, _raise_on_graphql_errors
from shopify_integration.shopify.utils import get_account_name, get_default_account

# constants
SYNC_JOB_NAME = "shopify.job.sync.all.products"
REALTIME_KEY = "shopify.key.sync.all.products"


def _get_account(shopify_account: str | None = None) -> str:
	"""Account the page works on: the one picked on the page, else the only enabled one.

	The page uses the account's stored credentials, so the caller needs read access to it.
	"""
	account = shopify_account or get_default_account().name
	frappe.has_permission(ACCOUNT_DOCTYPE, "read", account, throw=True)

	if not frappe.db.get_value(ACCOUNT_DOCTYPE, account, "enable_shopify"):
		frappe.throw(_("Shopify Account {0} is not enabled.").format(frappe.bold(account)))
	return account


def _gid_to_id(gid) -> str:
	"""Extract the plain numeric id from a Shopify GraphQL global id, e.g.
	"gid://shopify/Product/123" -> "123"."""
	if not gid:
		return ""
	return str(gid).rsplit("/", 1)[-1]


def _build_product_search_query(search_term):
	"""Translate a single combined search-box value into a Shopify Admin API
	search-string (the `query:` DSL already used in order.py's
	_fetch_old_orders). Combines a wildcard title match with a numeric id
	match via OR.

	Shopify's search DSL treats `: " ( ) *` as syntax characters - live
	testing against shopify.localhost confirmed that a raw term containing
	one of these (e.g. "(Sample) Coconut Bar Soap") silently breaks the
	query and returns an unrelated broad result set instead of erroring, so
	those characters are stripped from the term before it's embedded in the
	wildcard clause. Plain alphanumeric wildcard terms (e.g. "title:*sam*")
	were confirmed to return precise, correct substring matches."""
	term = (search_term or "").strip()
	if not term:
		return None

	sanitized = re.sub(r'[:"()*]', " ", term)
	sanitized = " ".join(sanitized.split())
	if not sanitized:
		return None

	clauses = [f"title:*{sanitized}*"]
	if sanitized.isdigit():
		clauses.append(f"id:{sanitized}")

	return " OR ".join(clauses)


@frappe.whitelist()
def get_shopify_products(
	from_: str | None = None,
	limit: int = 20,
	search_term: str | None = None,
	shopify_account: str | None = None,
):
	limit = cint(limit) or 20
	if limit not in (20, 50):
		limit = 20

	query = _build_product_search_query(search_term)
	try:
		shopify_products = fetch_all_products(
			from_, limit=limit, query=query, shopify_account=_get_account(shopify_account)
		)
	except TransientProviderError as e:
		# a plain exception would surface as a generic server error on the page
		frappe.throw(e.message, title=_("Shopify is busy"))
	return shopify_products


def fetch_all_products(from_=None, limit=20, query=None, shopify_account: str | None = None):
	# format shopify collection for datatable

	collection = _fetch_products_from_shopify(
		from_, limit=limit, query=query, shopify_account=shopify_account
	)

	products = collection["products"]
	for product in products:
		product["synced"] = is_synced(product["id"], shopify_account)

	return {
		"products": products,
		"nextUrl": collection["next_cursor"],
		"prevUrl": collection["prev_cursor"],
	}


_PRODUCTS_LIST_QUERY = """
query products($first: Int, $after: String, $last: Int, $before: String, $query: String) {
	products(first: $first, after: $after, last: $last, before: $before, query: $query) {
		edges {
			node {
				id
				title
				updatedAt
				variants(first: 100) {
					edges {
						node {
							sku
						}
					}
					pageInfo {
						hasNextPage
						endCursor
					}
				}
			}
		}
		pageInfo {
			hasNextPage
			hasPreviousPage
			startCursor
			endCursor
		}
	}
}
"""


def _decode_cursor(from_):
	"""`from_` is an opaque token this module hands back to the frontend as
	nextUrl/prevUrl and receives back verbatim - encode direction into it
	("n:"/"p:" prefix) so a single `from_` argument can represent either a
	forward or backward cursor, matching the JS frontend's existing (REST
	next_page_url/previous_page_url based) calling contract exactly."""
	if not from_:
		return None, "next"
	if from_.startswith("n:"):
		return from_[2:], "next"
	if from_.startswith("p:"):
		return from_[2:], "prev"
	return None, "next"


_VARIANT_PAGE_QUERY = """
query product($id: ID!, $after: String) {
	product(id: $id) {
		variants(first: 100, after: $after) {
			edges {
				node {
					sku
				}
			}
			pageInfo {
				hasNextPage
				endCursor
			}
		}
	}
}
"""

# Follow variant cursors up to this many extra pages (100 variants each).
MAX_VARIANT_PAGES = 10


def _append_remaining_variants(product_gid, variants_block, variants):
	"""Add variants past the first page so products with 100+ variants are not truncated.

	Listing only needs SKUs for display, so this stops after MAX_VARIANT_PAGES
	extra pages and logs it; the import path (_fetch_variant_ids) has no cap.
	"""
	info = variants_block.get("pageInfo") or {}
	after = info.get("endCursor")
	if not info.get("hasNextPage") or not after:
		return variants
	for _page in range(MAX_VARIANT_PAGES):
		response = json.loads(GraphQL().execute(_VARIANT_PAGE_QUERY, {"id": product_gid, "after": after}))
		_raise_on_graphql_errors(response, f"variants of {product_gid}")
		block = ((response.get("data") or {}).get("product") or {}).get("variants") or {}
		variants.extend({"sku": v.get("node", {}).get("sku")} for v in block.get("edges", []))
		info = block.get("pageInfo") or {}
		if not info.get("hasNextPage") or not info.get("endCursor"):
			return variants
		after = info["endCursor"]
	frappe.logger("shopify_integration").warning(
		f"Variant listing for {product_gid} truncated after {MAX_VARIANT_PAGES + 1} pages"
	)
	return variants


@temp_shopify_session
def _fetch_products_from_shopify(from_=None, limit=20, query=None, shopify_account: str | None = None):
	cursor, direction = _decode_cursor(from_)

	if direction == "prev":
		variables = {"last": limit, "before": cursor, "query": query}
	else:
		variables = {"first": limit, "after": cursor, "query": query}

	response = json.loads(GraphQL().execute(_PRODUCTS_LIST_QUERY, variables))
	_raise_on_graphql_errors(response, "products")
	products_data = response.get("data", {}).get("products", {})

	products = []
	for edge in products_data.get("edges", []):
		node = edge.get("node") or {}
		variants_block = node.get("variants") or {}
		variants = [{"sku": v.get("node", {}).get("sku")} for v in variants_block.get("edges", [])]
		variants = _append_remaining_variants(node.get("id"), variants_block, variants)
		products.append(
			{
				"id": _gid_to_id(node.get("id")),
				"title": node.get("title"),
				"updated_at": node.get("updatedAt"),
				"variants": variants,
			}
		)

	page_info = products_data.get("pageInfo", {})

	return {
		"products": products,
		"next_cursor": f"n:{page_info['endCursor']}" if page_info.get("hasNextPage") else None,
		"prev_cursor": f"p:{page_info['startCursor']}" if page_info.get("hasPreviousPage") else None,
	}


@frappe.whitelist()
def get_product_count(shopify_account: str | None = None):
	shopify_account = _get_account(shopify_account)

	items = frappe.db.get_list("Item", {"variant_of": ["is", "not set"]})
	erpnext_count = len(items)

	sync_items = frappe.db.get_list(
		"Ecommerce Item",
		{"variant_of": ["is", "not set"], "integration": MODULE_NAME, "shopify_account": shopify_account},
	)
	synced_count = len(sync_items)

	try:
		shopify_count = get_shopify_product_count(shopify_account=shopify_account)
	except TransientProviderError as e:
		frappe.throw(e.message, title=_("Shopify is busy"))

	return {
		"shopifyCount": shopify_count,
		"syncedCount": synced_count,
		"erpnextCount": erpnext_count,
	}


_PRODUCTS_COUNT_QUERY = """
{
	productsCount {
		count
	}
}
"""


@temp_shopify_session
def get_shopify_product_count(shopify_account: str | None = None):
	response = json.loads(GraphQL().execute(_PRODUCTS_COUNT_QUERY))
	_raise_on_graphql_errors(response, "product count")
	return response.get("data", {}).get("productsCount", {}).get("count", 0)


@frappe.whitelist(methods=["POST"])
def sync_product(product: str | int, shopify_account: str | None = None):
	try:
		shopify_product = ShopifyProduct(product, shopify_account=_get_account(shopify_account))
		shopify_product.sync_product()

		return True
	except Exception:
		frappe.db.rollback()
		return False


@frappe.whitelist(methods=["POST"])
def resync_product(product: str | int, shopify_account: str | None = None):
	return _resync_product(product, shopify_account=_get_account(shopify_account))


_PRODUCT_VARIANTS_QUERY = """
query product($id: ID!, $after: String) {
	product(id: $id) {
		variants(first: 100, after: $after) {
			edges {
				node {
					id
				}
			}
			pageInfo {
				hasNextPage
				endCursor
			}
		}
	}
}
"""


def _fetch_variant_ids(product_id) -> list[str] | None:
	"""Every variant id of a product, following cursors; None when the product does not exist.

	Must run inside a Shopify session. Raises on GraphQL errors so a failed or
	throttled call is never mistaken for "no variants".
	"""
	gid = f"gid://shopify/Product/{product_id}"
	variant_ids: list[str] = []
	after = None
	while True:
		response = json.loads(GraphQL().execute(_PRODUCT_VARIANTS_QUERY, {"id": gid, "after": after}))
		_raise_on_graphql_errors(response, f"variants of product {product_id}")
		product = (response.get("data") or {}).get("product")
		if not product:
			return None
		block = product.get("variants") or {}
		variant_ids.extend(_gid_to_id((edge.get("node") or {}).get("id")) for edge in block.get("edges", []))
		info = block.get("pageInfo") or {}
		if not info.get("hasNextPage") or not info.get("endCursor"):
			return variant_ids
		after = info["endCursor"]


@temp_shopify_session
def _resync_product(product, shopify_account: str | None = None):
	savepoint = "shopify_resync_product"
	try:
		variant_ids = _fetch_variant_ids(product) or []

		frappe.db.savepoint(savepoint)
		for variant_id in variant_ids:
			shopify_product = ShopifyProduct(product, variant_id=variant_id, shopify_account=shopify_account)
			shopify_product.sync_product()

		return True
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		return False


def is_synced(product, shopify_account: str | None = None):
	account = get_account_name(shopify_account)
	return ecommerce_item.is_synced(
		MODULE_NAME,
		integration_item_code=product,
		filters={"shopify_account": account} if account else None,
	)


@frappe.whitelist(methods=["POST"])
def import_all_products(shopify_account: str | None = None):
	shopify_account = _get_account(shopify_account)
	frappe.enqueue(
		queue_sync_all_products,
		queue="long",
		job_name=f"{SYNC_JOB_NAME}.{shopify_account}",
		shopify_account=shopify_account,
	)


def queue_sync_all_products(*args, shopify_account: str | None = None, **kwargs):
	start_time = process_time()
	shopify_account = _get_account(shopify_account)

	counts = get_product_count(shopify_account)
	publish("Syncing all products...", shopify_account=shopify_account)

	if counts["shopifyCount"] < counts["syncedCount"]:
		publish("⚠ Shopify has less products than ERPNext.", shopify_account=shopify_account)

	_sync = True
	try:
		collection = _fetch_products_from_shopify(limit=100, shopify_account=shopify_account)
	except TransientProviderError as e:
		return _stop_sync_on_throttle(e, shopify_account)
	savepoint = "shopify_product_sync"
	while _sync:
		for product in collection["products"]:
			try:
				publish(f"Syncing product {product['id']}", br=False, shopify_account=shopify_account)
				frappe.db.savepoint(savepoint)
				if is_synced(product["id"], shopify_account):
					publish(
						f"Product {product['id']} already synced. Skipping...",
						shopify_account=shopify_account,
					)
					continue

				shopify_product = ShopifyProduct(product["id"], shopify_account=shopify_account)
				shopify_product.sync_product()

				publish(f"✅ Synced Product {product['id']}", synced=True, shopify_account=shopify_account)

			except UniqueValidationError as e:
				publish(
					f"❌ Error Syncing Product {product['id']} : {e!s}",
					error=True,
					shopify_account=shopify_account,
				)
				frappe.db.rollback(save_point=savepoint)
				continue

			except Exception as e:
				publish(
					f"❌ Error Syncing Product {product['id']} : {e!s}",
					error=True,
					shopify_account=shopify_account,
				)
				frappe.db.rollback(save_point=savepoint)
				continue

		if collection["next_cursor"]:
			frappe.db.commit()  # prevents too many write request error
			try:
				collection = _fetch_products_from_shopify(
					from_=collection["next_cursor"], limit=100, shopify_account=shopify_account
				)
			except TransientProviderError as e:
				return _stop_sync_on_throttle(e, shopify_account)
		else:
			_sync = False

	end_time = process_time()
	publish(f"🎉 Done in {end_time - start_time}s", done=True, shopify_account=shopify_account)
	return True


def _stop_sync_on_throttle(exc, shopify_account):
	"""End the import with a readable message instead of leaving the page waiting."""
	publish(f"❌ {exc.message}", error=True, shopify_account=shopify_account)
	publish(_("Import stopped; run it again to continue."), done=True, shopify_account=shopify_account)
	return False


def publish(message, synced=False, error=False, done=False, br=True, shopify_account=None):
	frappe.publish_realtime(
		f"{REALTIME_KEY}.{shopify_account}",
		{
			"synced": synced,
			"error": error,
			"message": message + ("<br /><br />" if br else ""),
			"done": done,
		},
		# the job runs as the user who started the import
		user=frappe.session.user,
	)
