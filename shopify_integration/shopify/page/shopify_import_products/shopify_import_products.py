import json
from time import process_time

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from frappe.exceptions import UniqueValidationError
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import MODULE_NAME
from shopify_integration.shopify.product import ShopifyProduct
from shopify_integration.shopify.utils import create_shopify_log

# constants
SYNC_JOB_NAME = "shopify.job.sync.all.products"
REALTIME_KEY = "shopify.key.sync.all.products"


@frappe.whitelist()
def get_shopify_products(cursor: str | None = None, direction: str = "next"):
	shopify_products = fetch_all_products(cursor=cursor, direction=direction)
	return shopify_products


def fetch_all_products(cursor=None, direction="next"):
	"""Fetch paginated Shopify products."""

	response = _fetch_products_from_shopify(cursor=cursor, direction=direction)
	products_data = response.get("products", [])
	page_info = response.get("pageInfo", {})

	products = []

	for product in products_data:
		product["synced"] = is_synced(product["id"])
		products.append(product)

	return {
		"products": products,
		"nextCursor": page_info.get("endCursor"),
		"prevCursor": page_info.get("startCursor"),
		"pageInfo": {
			"hasNextPage": page_info.get("hasNextPage", False),
			"hasPreviousPage": page_info.get("hasPreviousPage", False),
		},
	}


@temp_shopify_session
def _fetch_products_from_shopify(cursor=None, direction="next", limit=20):
	"""
	Fetch products from Shopify with bidirectional pagination (forward/backward).

	Args:
	    cursor (str): Cursor for pagination.
	    direction (str): 'next' for forward, 'prev' for backward pagination.
	    limit (int): Number of products per page.

	Returns:
	    dict: {
	        "products": [...],
	        "pageInfo": {
	            "hasNextPage": bool,
	            "hasPreviousPage": bool,
	            "startCursor": str,
	            "endCursor": str
	        }
	    }
	"""

	if direction == "prev":
		query = """
        query ($last: Int!, $before: String) {
          products(last: $last, before: $before) {
            edges {
              cursor
              node {
                id
                title
                variants(first: 100) {
                  edges {
                    node {
                      id
                      title
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
		variables = {"last": limit, "before": cursor if cursor else None}
	else:
		query = """
        query ($first: Int!, $after: String) {
          products(first: $first, after: $after) {
            edges {
              cursor
              node {
                id
                title
                variants(first: 100) {
                  edges {
                    node {
                      id
                      title
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
		variables = {"first": limit, "after": cursor if cursor else None}

	response = GraphQL().execute(query, variables=variables)
	response_dict = json.loads(response)
	products_data = response_dict.get("data", {}).get("products", {})

	edges = products_data.get("edges", [])
	products = []

	for edge in edges:
		node = edge.get("node", {})

		product_id = node.get("id", "").split("/")[-1]

		variants = []
		for v in node.get("variants", {}).get("edges", []):
			variant_node = v.get("node", {})
			variant_id = variant_node.get("id", "").split("/")[-1]
			variants.append(
				{
					"id": variant_id,
					"title": variant_node.get("title"),
					"sku": variant_node.get("sku"),
				}
			)

		products.append({"id": product_id, "title": node.get("title"), "variants": variants})

	page_info = products_data.get("pageInfo", {})

	return {
		"products": products,
		"pageInfo": page_info,
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


@temp_shopify_session
def get_shopify_product_count():
	query = """
    query {
      productsCount {
        count
      }
    }
    """
	response = GraphQL().execute(query)
	result = json.loads(response)
	return result.get("data", {}).get("productsCount", {}).get("count", 0)


@frappe.whitelist()
def sync_product(product: str):
	try:
		shopify_product = ShopifyProduct(product)
		shopify_product.sync_product()

		return True
	except Exception:
		frappe.db.rollback()
		return False


@frappe.whitelist()
def resync_product(product: str):
	return _resync_product(product)


@temp_shopify_session
def _resync_product(product):
	savepoint = "shopify_resync_product"
	try:
		query = """
        query($id: ID!) {
          product(id: $id) {
            id
            variants(first: 250) {
              edges {
                node {
                  id
                  legacyResourceId
                }
              }
            }
          }
        }
        """
		product_gid = f"gid://shopify/Product/{product}"
		response = GraphQL().execute(query, variables={"id": product_gid})
		result = json.loads(response)
		product_data = result.get("data", {}).get("product")

		if not product_data:
			raise frappe.DoesNotExistError(f"Shopify product {product} not found")

		frappe.db.savepoint(savepoint)
		for edge in product_data.get("variants", {}).get("edges", []):
			variant_node = edge.get("node", {})
			variant_id = variant_node.get("legacyResourceId") or variant_node.get("id", "").split("/")[-1]
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
		publish("Shopify has less products than ERPNext.")

	_sync = True
	cursor = None
	savepoint = "shopify_product_sync"
	while _sync:
		response = _fetch_products_from_shopify(cursor=cursor, limit=100)
		products = response.get("products", [])
		page_info = response.get("pageInfo", {})

		for product in products:
			product_id = product["id"]
			try:
				publish(f"Syncing product {product_id}", br=False)
				frappe.db.savepoint(savepoint)
				if is_synced(product_id):
					publish(f"Product {product_id} already synced. Skipping...")
					continue

				shopify_product = ShopifyProduct(product_id)
				shopify_product.sync_product()

				publish(f"Synced Product {product_id}", synced=True)

			except UniqueValidationError as e:
				publish(f"Error Syncing Product {product_id} : {e!s}", error=True)
				frappe.db.rollback(save_point=savepoint)
				continue

			except Exception as e:
				publish(f"Error Syncing Product {product_id} : {e!s}", error=True)
				frappe.db.rollback(save_point=savepoint)
				continue

		if page_info.get("hasNextPage"):
			frappe.db.commit()  # prevents too many write request error  # nosemgrep: frappe-manual-commit
			cursor = page_info.get("endCursor")
		else:
			_sync = False

	end_time = process_time()
	duration = end_time - start_time
	publish(f"Done in {duration}s", done=True)

	create_shopify_log(
		method="queue_sync_all_products",
		status="Success",
		message=f"Synced all products in {duration}s",
	)
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
		user=frappe.session.user,
	)
