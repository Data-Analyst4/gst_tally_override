import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from gst_tally_override.overrides.sales_invoice_tax import (
	apply_sales_invoice_gst_override,
	calculate_item_gst_amounts,
	round_half,
	sync_tax_row_item_wise_details,
)

GST_PATCHES = {
	"get_item_tax_template_name": patch(
		"gst_tally_override.overrides.sales_invoice_tax.get_item_tax_template_name",
		return_value="GST-18%-TEST",
	),
	"get_gst_rate_from_template": patch(
		"gst_tally_override.overrides.sales_invoice_tax.get_gst_rate_from_template",
		return_value=18.0,
	),
}


class TestGSTOverride(FrappeTestCase):
	def setUp(self):
		self.company = "K95 Foods Private Limited"
		self.item_code = "GST-ITEM-18"

	def _intra_invoice(self, is_return=False, qty=1, rate=1000, customer_gstin="06AAICK4821A1ZZ"):
		inv = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": "_Test Customer",
				"is_return": is_return,
				"posting_date": "2025-12-04",
				"billing_address_gstin": customer_gstin,
				"net_total": qty * rate,
				"base_net_total": qty * rate,
				"total": qty * rate,
				"base_total": qty * rate,
			}
		)
		inv.append("items", {"item_code": self.item_code, "qty": qty, "rate": rate})
		inv.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "Output CGST - KFPL",
				"gst_tax_type": "cgst",
				"rate": 0,
			},
		)
		inv.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "Output SGST - KFPL",
				"gst_tax_type": "sgst",
				"rate": 0,
			},
		)
		return inv

	@GST_PATCHES["get_item_tax_template_name"]
	@GST_PATCHES["get_gst_rate_from_template"]
	def test_round_half_qty_rate(self, _rate, _template):
		item = frappe._dict(qty=180, rate=42.07, item_code=self.item_code)
		invoice = frappe._dict(
			company=self.company,
			name="TEST",
			billing_address_gstin="06AAICK4821A1ZZ",
		)

		amounts = calculate_item_gst_amounts(item, invoice)
		line_base = 180 * 42.07
		expected = round_half(line_base * 9 / 100, 2)

		self.assertEqual(amounts["cgst_amount"], expected)
		self.assertEqual(amounts["sgst_amount"], expected)

	@GST_PATCHES["get_item_tax_template_name"]
	@GST_PATCHES["get_gst_rate_from_template"]
	def test_intra_state_cgst_sgst(self, _rate, _template):
		inv = self._intra_invoice()
		apply_sales_invoice_gst_override(inv)

		self.assertEqual(float(inv.items[0].cgst_amount), 90.0)
		self.assertEqual(float(inv.items[0].sgst_amount), 90.0)
		self.assertEqual(float(inv.total_taxes_and_charges or 0), 180.0)
		self.assertEqual(float(inv.grand_total or 0), 1180.0)

	@GST_PATCHES["get_item_tax_template_name"]
	@GST_PATCHES["get_gst_rate_from_template"]
	def test_tax_row_item_wise_detail_matches_items(self, _rate, _template):
		inv = self._intra_invoice()
		apply_sales_invoice_gst_override(inv)

		for tax in inv.taxes:
			if tax.gst_tax_type not in ("cgst", "sgst"):
				continue

			detail = json.loads(tax.item_wise_tax_detail or "{}")
			item = inv.items[0]
			rate = float(item.get(f"{tax.gst_tax_type}_rate") or 0)
			amount = float(item.get(f"{tax.gst_tax_type}_amount") or 0)

			self.assertEqual(detail[item.item_code], [rate, amount])
			self.assertEqual(rate, 9.0)
			self.assertNotEqual(rate, 40.0)

	@GST_PATCHES["get_item_tax_template_name"]
	@GST_PATCHES["get_gst_rate_from_template"]
	def test_credit_note_negative_tax(self, _rate, _template):
		inv = self._intra_invoice(is_return=True, qty=-1, rate=1000)
		apply_sales_invoice_gst_override(inv)

		self.assertEqual(float(inv.items[0].cgst_amount), -90.0)
		self.assertEqual(float(inv.items[0].sgst_amount), -90.0)
		self.assertEqual(float(inv.total_taxes_and_charges or 0), -180.0)
		self.assertTrue(bool(getattr(inv.flags, "skip_gst_validations", False)))

	@GST_PATCHES["get_item_tax_template_name"]
	@GST_PATCHES["get_gst_rate_from_template"]
	def test_inter_state_igst(self, _rate, _template):
		inv = self._intra_invoice(customer_gstin="27AABCU9603R1Z2")
		inv.taxes = []
		inv.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "Output IGST - KFPL",
				"gst_tax_type": "igst",
				"rate": 0,
			},
		)
		apply_sales_invoice_gst_override(inv)

		self.assertEqual(float(inv.items[0].igst_amount), 180.0)
		self.assertEqual(float(inv.items[0].cgst_amount), 0.0)
		self.assertEqual(float(inv.total_taxes_and_charges or 0), 180.0)

	@GST_PATCHES["get_item_tax_template_name"]
	@GST_PATCHES["get_gst_rate_from_template"]
	def test_sync_tax_row_item_wise_details(self, _rate, _template):
		inv = self._intra_invoice()
		apply_sales_invoice_gst_override(inv)

		for tax in inv.taxes:
			self.assertEqual(tax.dont_recompute_tax, 1)
			if tax.gst_tax_type in ("cgst", "sgst"):
				detail = json.loads(tax.item_wise_tax_detail or "{}")
				self.assertIn(inv.items[0].item_code, detail)

	def test_round_half_negative(self):
		self.assertEqual(round_half(-1.235, 2), -1.24)
		self.assertEqual(round_half(1.235, 2), 1.24)
