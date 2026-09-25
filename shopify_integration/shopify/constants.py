# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE


MODULE_NAME = "shopify"
SETTING_DOCTYPE = "Shopify Setting"
OLD_SETTINGS_DOCTYPE = "Shopify Settings"

API_VERSION = "2024-01"

# Authentication methods (must match the Select options in Shopify Setting)
AUTH_METHOD_STATIC = "Static Token"
AUTH_METHOD_OAUTH = "OAuth 2.0 Client Credentials"

# Admin API access scopes the connector's GraphQL operations and webhook
# topics need. Configured on the app in the Shopify Dev Dashboard; a token
# only carries the scopes its app version had when it was issued.
REQUIRED_ACCESS_SCOPES = (
	"read_orders",  # order webhook topics + historical order import
	"read_products",  # product / variant queries
	"write_products",  # productCreate / productUpdate / variants bulk mutations
	"read_locations",  # locations query (Fetch Shopify Locations)
	"read_inventory",  # inventory queries
	"write_inventory",  # inventoryActivate / inventorySetQuantities
)

# Optional Shopify approval scope needed only for imports older than 60 days.
HISTORICAL_ORDERS_ACCESS_SCOPE = "read_all_orders"

# Shopify's GraphQL webhookSubscriptionCreate mutation expects topics as
# WebhookSubscriptionTopic enum values, unlike REST's "orders/create" style.
WEBHOOK_EVENTS = [
	"ORDERS_CREATE",
	"ORDERS_PAID",
	"ORDERS_FULFILLED",
	"ORDERS_CANCELLED",
	"ORDERS_PARTIALLY_FULFILLED",
]

EVENT_MAPPER = {
	"orders/create": "shopify_integration.shopify.order.sync_sales_order",
	"orders/paid": "shopify_integration.shopify.invoice.prepare_sales_invoice",
	"orders/fulfilled": "shopify_integration.shopify.fulfillment.prepare_delivery_note",
	"orders/cancelled": "shopify_integration.shopify.order.cancel_order",
	"orders/partially_fulfilled": "shopify_integration.shopify.fulfillment.prepare_delivery_note",
}

SHOPIFY_VARIANTS_ATTR_LIST = ["option1", "option2", "option3"]

# custom fields

CUSTOMER_ID_FIELD = "shopify_customer_id"
ORDER_ID_FIELD = "shopify_order_id"
ORDER_NUMBER_FIELD = "shopify_order_number"
ORDER_STATUS_FIELD = "shopify_order_status"
FULLFILLMENT_ID_FIELD = "shopify_fulfillment_id"
SUPPLIER_ID_FIELD = "shopify_supplier_id"
ADDRESS_ID_FIELD = "shopify_address_id"
ORDER_ITEM_DISCOUNT_FIELD = "shopify_item_discount"
ITEM_SELLING_RATE_FIELD = "shopify_selling_rate"

# ERPNext already defines the default UOMs from Shopify but names are different
# Shopify's GraphQL weightUnit enum values are uppercase (e.g. "KILOGRAMS"),
# unlike REST's lowercase abbreviations (e.g. "kg").
WEIGHT_TO_ERPNEXT_UOM_MAP = {
	"KILOGRAMS": "Kg",
	"GRAMS": "Gram",
	"OUNCES": "Ounce",
	"POUNDS": "Pound",
}
