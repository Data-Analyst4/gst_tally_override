import json
import types

import frappe
from frappe.utils import floor, flt
from india_compliance.gst_india.overrides.transaction import update_taxable_values

GST_OVERRIDE_DOCTYPE = "Sales Invoice"
GST_TAX_TYPES = ("cgst", "sgst", "igst", "cess")


def uses_tally_gst_override(doc):
	return doc.doctype == GST_OVERRIDE_DOCTYPE


def should_skip_ic_gst(doc):
	return uses_tally_gst_override(doc) and bool(getattr(doc.flags, "skip_gst_validations", False))


def _patch_india_compliance():
	"""Monkey-patch India Compliance so Tally override can run on Sales Invoice."""
	try:
		import india_compliance.gst_india.overrides.sales_invoice as ic_si
		import india_compliance.gst_india.overrides.transaction as ic_txn

		if not getattr(ic_txn, "_gst_tally_override_patched", False):
			if hasattr(ic_txn, "validate_item_wise_tax_detail"):
				_orig_item_validate = ic_txn.validate_item_wise_tax_detail

				def patched_validate_item_wise_tax_detail(doc):
					if should_skip_ic_gst(doc):
						return
					return _orig_item_validate(doc)

				ic_txn.validate_item_wise_tax_detail = patched_validate_item_wise_tax_detail

			if hasattr(ic_txn, "validate_transaction"):
				_orig_txn_validate = ic_txn.validate_transaction

				def patched_validate_transaction(doc, method=None):
					if should_skip_ic_gst(doc):
						return False
					return _orig_txn_validate(doc, method)

				ic_txn.validate_transaction = patched_validate_transaction

			if hasattr(ic_txn, "set_item_wise_tax_breakup"):
				_orig_set_breakup = ic_txn.set_item_wise_tax_breakup

				def patched_set_item_wise_tax_breakup(doc, *args, **kwargs):
					if should_skip_ic_gst(doc):
						return
					return _orig_set_breakup(doc, *args, **kwargs)

				ic_txn.set_item_wise_tax_breakup = patched_set_item_wise_tax_breakup

			if hasattr(ic_txn, "ItemGSTDetails"):
				_OrigItemGSTDetails = ic_txn.ItemGSTDetails

				class PatchedItemGSTDetails(_OrigItemGSTDetails):
					def update(self, doc):
						if should_skip_ic_gst(doc):
							return
						return super().update(doc)

					def validate_item_gst_details(self):
						if should_skip_ic_gst(self.doc):
							return
						return super().validate_item_gst_details()

				ic_txn.ItemGSTDetails = PatchedItemGSTDetails

			if hasattr(ic_txn, "update_gst_details"):
				_orig_update_gst_details = ic_txn.update_gst_details

				def patched_update_gst_details(doc, method=None):
					if should_skip_ic_gst(doc):
						return
					return _orig_update_gst_details(doc, method)

				ic_txn.update_gst_details = patched_update_gst_details

			ic_txn._gst_tally_override_patched = True

		if hasattr(ic_si, "validate_transaction") and not getattr(
			ic_si, "_gst_tally_override_patched", False
		):
			_orig_si_validate_transaction = ic_si.validate_transaction

			def patched_si_validate_transaction(doc, method=None):
				if should_skip_ic_gst(doc):
					return False
				return _orig_si_validate_transaction(doc, method)

			ic_si.validate_transaction = patched_si_validate_transaction
			ic_si._gst_tally_override_patched = True

	except Exception:
		frappe.log_error(title="gst_tally_override: India Compliance patch failed")


_patch_india_compliance()


def round_half(n, decimals=2):
	"""Tally-style rounding: half away from zero to given decimals."""
	multiplier = 10**decimals
	if n < 0:
		return float(int(n * multiplier - 0.5)) / multiplier
	return float(int(n * multiplier + 0.5)) / multiplier


def round_half_up(n):
	"""Round half up to nearest integer rupee."""
	decimal_part = n - int(n)

	if n >= 0:
		return int(n) + 1 if decimal_part >= 0.5 else int(n)
	return int(n) - 1 if decimal_part <= -0.5 else int(n)


def get_item_tax_template_name(item_code):
	try:
		item_doc = frappe.get_cached_doc("Item", item_code)

		if hasattr(item_doc, "taxes") and item_doc.taxes:
			return item_doc.taxes[0].item_tax_template

		return None
	except Exception as e:
		frappe.logger().error(f"Error fetching Item Tax Template for {item_code}: {str(e)}")
		return None


def get_gst_rate_from_template(template_name, company):
	if not template_name:
		return 0.0

	try:
		template = frappe.get_cached_doc("Item Tax Template", template_name)

		if hasattr(template, "gst_rate"):
			return float(template.gst_rate or 0)

		return 0.0

	except Exception as e:
		frappe.logger().error(f"Error fetching GST rate from template {template_name}: {str(e)}")
		return 0.0


def get_item_gst_rate(item, invoice_doc):
	template_name = get_item_tax_template_name(item.item_code)
	if not template_name:
		frappe.logger().warning(
			f"No Item Tax Template found for {item.item_code} in invoice {invoice_doc.name}"
		)
		return 0.0, True

	gst_rate = get_gst_rate_from_template(template_name, invoice_doc.company)
	if gst_rate == 0:
		frappe.logger().warning(
			f"GST rate is 0 for template {template_name} on item {item.item_code}"
		)
	is_inter_state = check_if_inter_state(invoice_doc)
	return float(gst_rate or 0), is_inter_state


def calculate_item_gst_amounts(item, invoice_doc):
	"""Per-line GST on qty * rate with Tally-style rounding."""
	qty = float(item.qty or 0)
	rate = float(item.rate or 0)
	line_base = qty * rate

	template_name = get_item_tax_template_name(item.item_code)

	if not template_name:
		frappe.logger().warning(
			f"No Item Tax Template found for {item.item_code} in invoice {invoice_doc.name}"
		)
		return {"cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0}

	gst_rate = get_gst_rate_from_template(template_name, invoice_doc.company)

	if gst_rate == 0:
		frappe.logger().warning(
			f"GST rate is 0 for template {template_name} on item {item.item_code}"
		)
		return {"cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0}

	is_inter_state = check_if_inter_state(invoice_doc)

	if is_inter_state:
		igst_amount = round_half(line_base * gst_rate / 100, 2)
		cgst_amount = 0.0
		sgst_amount = 0.0
	else:
		half_rate = gst_rate / 2
		cgst_amount = round_half(line_base * half_rate / 100, 2)
		sgst_amount = round_half(line_base * half_rate / 100, 2)
		igst_amount = 0.0

	frappe.logger().debug(
		f"Item {item.item_code}: Template={template_name}, Rate={gst_rate}%, "
		f"base={line_base}, CGST={cgst_amount}, SGST={sgst_amount}, IGST={igst_amount}"
	)

	return {
		"cgst_amount": cgst_amount,
		"sgst_amount": sgst_amount,
		"igst_amount": igst_amount,
	}


def check_if_inter_state(invoice_doc):
	try:
		company_gstin = frappe.get_cached_value("Company", invoice_doc.company, "gstin")
		customer_gstin = invoice_doc.billing_address_gstin or invoice_doc.customer_gstin or ""

		if not company_gstin or not customer_gstin:
			return True

		return company_gstin[:2] != customer_gstin[:2]

	except Exception as e:
		frappe.logger().error(f"Error checking inter-state: {str(e)}")
		return True


def set_item_gst_rates(item, invoice_doc):
	gst_rate, is_inter_state = get_item_gst_rate(item, invoice_doc)

	if is_inter_state:
		item.igst_rate = gst_rate
		item.cgst_rate = 0
		item.sgst_rate = 0
	else:
		half = gst_rate / 2.0
		item.igst_rate = 0
		item.cgst_rate = half
		item.sgst_rate = half


def sync_tax_row_item_wise_details(doc):
	"""Keep taxes[].item_wise_tax_detail aligned with per-line GST fields."""
	for tax in doc.taxes:
		gst_type = (tax.gst_tax_type or "").lower()
		if gst_type not in GST_TAX_TYPES:
			continue

		detail = {}
		for item in doc.items:
			rate = float(getattr(item, f"{gst_type}_rate", 0) or 0)
			amount = float(getattr(item, f"{gst_type}_amount", 0) or 0)
			if rate or amount:
				detail[item.item_code] = [rate, amount]

		tax.item_wise_tax_detail = json.dumps(detail)
		tax.dont_recompute_tax = 1


def update_tax_rows_and_totals(doc, total_cgst, total_sgst, total_igst):
	total_tax_amount = total_cgst + total_sgst + total_igst

	doc.total_taxes_and_charges = total_tax_amount
	doc.base_total_taxes_and_charges = total_tax_amount

	net = float(doc.net_total or 0)
	base_net = float(doc.base_total or 0)

	grand_total = net + total_tax_amount
	base_grand_total = base_net + total_tax_amount

	decimal_part = base_grand_total - floor(base_grand_total)
	base_rounded_total = (
		floor(base_grand_total) + 1 if decimal_part >= 0.5 else floor(base_grand_total)
	)
	base_rounding_adjustment = base_rounded_total - base_grand_total

	running_total = base_net

	for tax in doc.taxes:
		tax.dont_recompute_tax = 1
		tt = ((tax.get("gst_tax_type") or "") or (tax.get("account_head") or "")).lower()

		if "cgst" in tt:
			tax.tax_amount = total_cgst
			tax.base_tax_amount = total_cgst
			tax.tax_amount_after_discount_amount = total_cgst
			tax.base_tax_amount_after_discount_amount = total_cgst
			running_total += total_cgst

		elif "sgst" in tt or "utgst" in tt:
			tax.tax_amount = total_sgst
			tax.base_tax_amount = total_sgst
			tax.tax_amount_after_discount_amount = total_sgst
			tax.base_tax_amount_after_discount_amount = total_sgst
			running_total += total_sgst

		elif "igst" in tt:
			tax.tax_amount = total_igst
			tax.base_tax_amount = total_igst
			tax.tax_amount_after_discount_amount = total_igst
			tax.base_tax_amount_after_discount_amount = total_igst
			running_total += total_igst

		tax.total = running_total
		tax.base_total = running_total

	doc.grand_total = grand_total
	doc.base_grand_total = base_grand_total
	doc.rounding_adjustment = base_rounding_adjustment
	doc.base_rounding_adjustment = base_rounding_adjustment
	doc.rounded_total = round_half_up(grand_total)
	doc.base_rounded_total = base_rounded_total
	doc.outstanding_amount = base_rounded_total


def _noop_calculate_taxes_and_totals(self):
	"""Module-level no-op so document stays pickle-safe for webhooks/background jobs."""


def block_tax_recalculation(doc):
	doc.flags.ignore_validate_update_after_submit = True
	doc.flags.dont_update_if_missing = True
	doc.flags.dont_recalculate_taxes = True
	doc.calculate_taxes_and_totals = types.MethodType(_noop_calculate_taxes_and_totals, doc)


def sync_item_taxable_values(doc):
	"""Set item.taxable_value so e-Invoice AssAmt matches per-line GST amounts."""
	update_taxable_values(doc)

	for item in doc.items:
		if flt(item.taxable_value):
			continue

		item.taxable_value = flt(
			item.base_net_amount or item.net_amount or flt(item.qty) * flt(item.rate),
			item.precision("taxable_value"),
		)


def apply_sales_invoice_gst_override(doc):
	"""Tally-style per-line GST, tax table sync, and totals for SI / credit notes."""
	if not uses_tally_gst_override(doc) or not doc.items or not doc.taxes:
		return

	if doc.docstatus == 2:
		return

	doc.flags.skip_gst_validations = True
	doc.flags.ignore_mandatory = True
	doc.flags.dont_validate_item_tax_template = True
	doc.flags.skip_item_tax_calculation = True

	for item in doc.items:
		gst_amounts = calculate_item_gst_amounts(item, doc)
		item.cgst_amount = gst_amounts["cgst_amount"]
		item.sgst_amount = gst_amounts["sgst_amount"]
		item.igst_amount = gst_amounts["igst_amount"]
		set_item_gst_rates(item, doc)

	total_cgst = sum(float(getattr(i, "cgst_amount", 0) or 0) for i in doc.items)
	total_sgst = sum(float(getattr(i, "sgst_amount", 0) or 0) for i in doc.items)
	total_igst = sum(float(getattr(i, "igst_amount", 0) or 0) for i in doc.items)

	update_tax_rows_and_totals(doc, total_cgst, total_sgst, total_igst)
	sync_item_taxable_values(doc)
	sync_tax_row_item_wise_details(doc)
	block_tax_recalculation(doc)

	frappe.logger().debug(
		f"[GST Override] {doc.name}: CGST={total_cgst}, SGST={total_sgst}, "
		f"IGST={total_igst}, total_taxes_and_charges={doc.total_taxes_and_charges}"
	)


def on_before_validate(doc, method=None):
	apply_sales_invoice_gst_override(doc)


on_validate = on_before_validate


def on_before_submit(doc, method=None):
	apply_sales_invoice_gst_override(doc)
