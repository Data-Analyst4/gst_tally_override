"""Repair taxable_value on submitted Sales Invoices (no UI amend required)."""

import frappe
from frappe import _
from frappe.utils import flt

from gst_tally_override.overrides.sales_invoice_tax import (
	has_inflated_taxable_value,
	repair_doc_taxable_values,
)


@frappe.whitelist()
def repair_sales_invoice(sales_invoice, dry_run=True):
	"""
	Fix item.taxable_value and GST breakup on a submitted Sales Invoice.

	bench --site SITE execute gst_tally_override.utils.repair_submitted_invoice.repair_sales_invoice \\
	    --kwargs '{"sales_invoice": "K95/26-27/001627", "dry_run": false}'
	"""
	dry_run = frappe.parse_json(dry_run) if isinstance(dry_run, str) else dry_run
	return _repair_one(sales_invoice, dry_run=bool(dry_run))


@frappe.whitelist()
def repair_sales_invoices(sales_invoices=None, dry_run=True):
	"""Repair multiple invoices or auto-detect inflated taxable values."""
	dry_run = frappe.parse_json(dry_run) if isinstance(dry_run, str) else dry_run
	dry_run = bool(dry_run)

	if sales_invoices:
		if isinstance(sales_invoices, str):
			sales_invoices = frappe.parse_json(sales_invoices)
		names = list(sales_invoices)
	else:
		names = find_invoices_with_inflated_taxable_value()

	results = []
	for name in names:
		try:
			results.append(_repair_one(name, dry_run=dry_run))
		except Exception:
			frappe.log_error(title=f"GST repair failed: {name}")
			results.append({"sales_invoice": name, "status": "error", "message": frappe.get_traceback()})

	return results


def find_invoices_with_inflated_taxable_value():
	"""Submitted SIs where sum(taxable_value) is much higher than base_net_total."""
	rows = frappe.db.sql(
		"""
		SELECT si.name
		FROM `tabSales Invoice` si
		INNER JOIN (
			SELECT parent,
				SUM(base_net_amount) AS net_total,
				SUM(taxable_value) AS taxable_total,
				SUM(IFNULL(igst_amount, 0) + IFNULL(cgst_amount, 0) + IFNULL(sgst_amount, 0)) AS tax_total
			FROM `tabSales Invoice Item`
			GROUP BY parent
		) agg ON agg.parent = si.name
		WHERE si.docstatus = 1
			AND IFNULL(si.irn, '') = ''
			AND agg.net_total > 0
			AND ABS(agg.taxable_total - agg.net_total - agg.tax_total) < 1
		ORDER BY si.modified DESC
		""",
		as_dict=True,
	)
	return [r.name for r in rows]


def _repair_one(sales_invoice, dry_run=True):
	doc = frappe.get_doc("Sales Invoice", sales_invoice)

	if doc.docstatus != 1:
		frappe.throw(_("Sales Invoice {0} must be submitted (docstatus 1)").format(sales_invoice))

	if doc.irn:
		frappe.throw(_("Sales Invoice {0} already has an IRN; cancel IRN before repair").format(sales_invoice))

	if not has_inflated_taxable_value(doc):
		return {
			"sales_invoice": sales_invoice,
			"status": "skipped",
			"message": _("taxable_value already matches base net amounts"),
		}

	changes = repair_doc_taxable_values(doc, persist=not dry_run)

	return {
		"sales_invoice": sales_invoice,
		"status": "dry_run" if dry_run else "repaired",
		"dry_run": dry_run,
		"changes": changes,
		"message": _("Would repair {0} item line(s)").format(len(changes))
		if dry_run
		else _("Repaired {0} item line(s). Regenerate e-Invoice from the invoice.").format(len(changes)),
	}
