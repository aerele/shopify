import json
import re
from time import process_time

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from frappe.exceptions import UniqueValidationError
from frappe.utils import cint
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import MODULE_NAME
from shopify_integration.shopify.product import ShopifyProduct

# constants
SYNC_JOB_NAME = "shopify.job.sync.all.products"
REALTIME_KEY = "shopify.key.sync.all.products"


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
def get_shopify_products(from_: str | None = None, limit: int = 20, search_term: str | None = None):
	limit = cint(limit) or 20
	if limit not in (20, 50):
		limit = 20

	query = _build_product_search_query(search_term)
	shopify_products = fetch_all_products(from_, limit=limit, query=query)
	return shopify_products


def fetch_all_products(from_=None, limit=20, query=None):
	# format shopify collection for datatable

	collection = _fetch_products_from_shopify(from_, limit=limit, query=query)

	products = collection["products"]
	for product in products:
		product["synced"] = is_synced(product["id"])

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
				variants(first: 100) {
					edges {
						node {
							sku
						}
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


@temp_shopify_session
def _fetch_products_from_shopify(from_=None, limit=20, query=None):
	cursor, direction = _decode_cursor(from_)

	if direction == "prev":
		variables = {"last": limit, "before": cursor, "query": query}
	else:
		variables = {"first": limit, "after": cursor, "query": query}

	response = json.loads(GraphQL().execute(_PRODUCTS_LIST_QUERY, variables))
	products_data = response.get("data", {}).get("products", {})

	products = []
	for edge in products_data.get("edges", []):
		node = edge.get("node") or {}
		variants = [
			{"sku": v.get("node", {}).get("sku")} for v in (node.get("variants") or {}).get("edges", [])
		]
		products.append(
			{
				"id": _gid_to_id(node.get("id")),
				"title": node.get("title"),
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
def get_product_count():
	items = frappe.db.get_list("Item", {"variant_of": ["is", "not set"]})
	erpnext_count = len(items)

	sync_items = frappe.db.get_list("Ecommerce Item", {"variant_of": ["is", "not set"]})
	synced_count = len(sync_items)

	shopify_count = get_shopify_product_count()

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
def get_shopify_product_count():
	response = json.loads(GraphQL().execute(_PRODUCTS_COUNT_QUERY))
	return response.get("data", {}).get("productsCount", {}).get("count", 0)


@frappe.whitelist()
def sync_product(product):
	try:
		shopify_product = ShopifyProduct(product)
		shopify_product.sync_product()

		return True
	except Exception:
		frappe.db.rollback()
		return False


@frappe.whitelist()
def resync_product(product):
	return _resync_product(product)


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


@temp_shopify_session
def _resync_product(product):
	savepoint = "shopify_resync_product"
	try:
		response = json.loads(
			GraphQL().execute(_PRODUCT_VARIANTS_QUERY, {"id": f"gid://shopify/Product/{product}"})
		)
		item = response.get("data", {}).get("product") or {}

		frappe.db.savepoint(savepoint)
		for edge in (item.get("variants") or {}).get("edges", []):
			variant_id = _gid_to_id((edge.get("node") or {}).get("id"))
			shopify_product = ShopifyProduct(product, variant_id=variant_id)
			shopify_product.sync_product()

		return True
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		return False


def is_synced(product):
	return ecommerce_item.is_synced(MODULE_NAME, integration_item_code=product)


@frappe.whitelist()
def import_all_products():
	frappe.enqueue(
		queue_sync_all_products,
		queue="long",
		job_name=SYNC_JOB_NAME,
		key=REALTIME_KEY,
	)


def queue_sync_all_products(*args, **kwargs):
	start_time = process_time()

	counts = get_product_count()
	publish("Syncing all products...")

	if counts["shopifyCount"] < counts["syncedCount"]:
		publish("⚠ Shopify has less products than ERPNext.")

	_sync = True
	collection = _fetch_products_from_shopify(limit=100)
	savepoint = "shopify_product_sync"
	while _sync:
		for product in collection["products"]:
			try:
				publish(f"Syncing product {product['id']}", br=False)
				frappe.db.savepoint(savepoint)
				if is_synced(product["id"]):
					publish(f"Product {product['id']} already synced. Skipping...")
					continue

				shopify_product = ShopifyProduct(product["id"])
				shopify_product.sync_product()

				publish(f"✅ Synced Product {product['id']}", synced=True)

			except UniqueValidationError as e:
				publish(f"❌ Error Syncing Product {product['id']} : {e!s}", error=True)
				frappe.db.rollback(save_point=savepoint)
				continue

			except Exception as e:
				publish(f"❌ Error Syncing Product {product['id']} : {e!s}", error=True)
				frappe.db.rollback(save_point=savepoint)
				continue

		if collection["next_cursor"]:
			frappe.db.commit()  # prevents too many write request error
			collection = _fetch_products_from_shopify(from_=collection["next_cursor"], limit=100)
		else:
			_sync = False

	end_time = process_time()
	publish(f"🎉 Done in {end_time - start_time}s", done=True)
	return True


def publish(message, synced=False, error=False, done=False, br=True):
	frappe.publish_realtime(
		REALTIME_KEY,
		{
			"synced": synced,
			"error": error,
			"message": message + ("<br /><br />" if br else ""),
			"done": done,
		},
	)
