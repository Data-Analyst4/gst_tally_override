import json
import frappe
from frappe import _
from frappe.utils import floor


# ========= DISABLE INDIA COMPLIANCE GST FOR SALES INVOICE =========
try:
    import india_compliance.gst_india.overrides.transaction as ic_txn

    # 1) Item-wise GST validation
    if hasattr(ic_txn, "validate_item_wise_tax_detail"):
        _orig_item_validate = ic_txn.validate_item_wise_tax_detail

        def patched_validate_item_wise_tax_detail(doc):
            if doc.doctype == "Sales Invoice" and getattr(doc.flags, "skip_gst_validations", False):
                return
            return _orig_item_validate(doc)

        ic_txn.validate_item_wise_tax_detail = patched_validate_item_wise_tax_detail

    # 2) Transaction validation
    if hasattr(ic_txn, "validate_transaction"):
        _orig_txn_validate = ic_txn.validate_transaction

        def patched_validate_transaction(doc, method=None):
            if doc.doctype == "Sales Invoice" and getattr(doc.flags, "skip_gst_validations", False):
                return
            return _orig_txn_validate(doc, method)

        ic_txn.validate_transaction = patched_validate_transaction

    # 3) Item-wise breakup builder
    if hasattr(ic_txn, "set_item_wise_tax_breakup"):
        _orig_set_breakup = ic_txn.set_item_wise_tax_breakup

        def patched_set_item_wise_tax_breakup(doc, *args, **kwargs):
            if doc.doctype == "Sales Invoice" and getattr(doc.flags, "skip_gst_validations", False):
                return
            return _orig_set_breakup(doc, *args, **kwargs)

        ic_txn.set_item_wise_tax_breakup = patched_set_breakup

    # 4) ItemGSTDetails.update
    if hasattr(ic_txn, "ItemGSTDetails"):
        _OrigItemGSTDetails = ic_txn.ItemGSTDetails

        class PatchedItemGSTDetails(_OrigItemGSTDetails):
            def update(self, doc):
                if doc.doctype == "Sales Invoice" and getattr(doc.flags, "skip_gst_validations", False):
                    return
                return super().update(doc)

        ic_txn.ItemGSTDetails = PatchedItemGSTDetails

    # 5) update_gst_details wrapper
    if hasattr(ic_txn, "update_gst_details"):
        _orig_update_gst_details = ic_txn.update_gst_details

        def patched_update_gst_details(doc, method=None):
            if doc.doctype == "Sales Invoice" and getattr(doc.flags, "skip_gst_validations", False):
                return
            return _orig_update_gst_details(doc, method)

        ic_txn.update_gst_details = patched_update_gst_details

except Exception:
    pass


# ========= ROUNDING HELPERS =========
def round_half(n, decimals=2):
    """Tally-style rounding: half away from zero to given decimals."""
    multiplier = 10 ** decimals
    if n < 0:
        return float(int(n * multiplier - 0.5)) / multiplier
    return float(int(n * multiplier + 0.5)) / multiplier


def round_half_up(n):
    """Round half up to nearest integer rupee."""
    decimal_part = n - int(n)

    if n >= 0:
        return int(n) + 1 if decimal_part >= 0.5 else int(n)
    else:
        return int(n) - 1 if decimal_part <= -0.5 else int(n)


# ========= GST RATE LOOKUP FUNCTIONS =========
def get_item_tax_template_name(item_code):
    """
    Get the Item Tax Template name from Item master.
    
    Args:
        item_code: Item code
        
    Returns:
        Template name (string) or None
    """
    try:
        item_doc = frappe.get_cached_doc("Item", item_code)
        
        if hasattr(item_doc, "taxes") and item_doc.taxes:
            # Get first Item Tax Template from taxes child table
            return item_doc.taxes[0].item_tax_template
            
        return None
    except Exception as e:
        frappe.logger().error(f"Error fetching Item Tax Template for {item_code}: {str(e)}")
        return None


def get_gst_rate_from_template(template_name, company):
    """
    Get GST rate from Item Tax Template.
    
    Args:
        template_name: Name of Item Tax Template
        company: Company name
        
    Returns:
        GST rate as float (e.g. 5.0, 12.0, 18.0)
    """
    if not template_name:
        return 0.0
        
    try:
        template = frappe.get_cached_doc("Item Tax Template", template_name)
        
        # Check if template has gst_rate field
        if hasattr(template, "gst_rate"):
            return float(template.gst_rate or 0)
            
        # If no gst_rate field, return 0
        return 0.0
        
    except Exception as e:
        frappe.logger().error(f"Error fetching GST rate from template {template_name}: {str(e)}")
        return 0.0

def get_item_gst_rate(item, invoice_doc):
    """Return gst_rate (combined %) and inter_state flag for an item."""
    template_name = get_item_tax_template_name(item.item_code)
    if not template_name:
        frappe.logger().warning(
            f"No Item Tax Template found for {item.item_code} in invoice {invoice_doc.name}"
        )
        return 0.0, True  # default IGST path with 0%

    gst_rate = get_gst_rate_from_template(template_name, invoice_doc.company)
    if gst_rate == 0:
        frappe.logger().warning(
            f"GST rate is 0 for template {template_name} on item {item.item_code}"
        )
    is_inter_state = check_if_inter_state(invoice_doc)
    return float(gst_rate or 0), is_inter_state


def calculate_item_gst_amounts(item, invoice_doc):
    """
    Calculate GST amounts for a single invoice item.
    Fetches rate from Item Tax Template and applies Tally-style rounding.
    
    Args:
        item: Sales Invoice Item row
        invoice_doc: Parent Sales Invoice document
        
    Returns:
        dict with cgst_amount, sgst_amount, igst_amount
    """
    qty = float(item.qty or 0)
    rate = float(item.rate or 0)
    
    # Get Item Tax Template
    template_name = get_item_tax_template_name(item.item_code)
    
    if not template_name:
        frappe.logger().warning(
            f"No Item Tax Template found for {item.item_code} in invoice {invoice_doc.name}"
        )
        return {"cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0}
    
    # Get GST rate from template
    gst_rate = get_gst_rate_from_template(template_name, invoice_doc.company)
    
    if gst_rate == 0:
        frappe.logger().warning(
            f"GST rate is 0 for template {template_name} on item {item.item_code}"
        )
        return {"cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0}
    
    # Determine if Inter-state (IGST) or Intra-state (CGST+SGST)
    is_inter_state = check_if_inter_state(invoice_doc)
    
    if is_inter_state:
        # IGST = full GST rate
        igst_amount = round_half(qty * rate * gst_rate / 100, 2)
        cgst_amount = 0.0
        sgst_amount = 0.0
    else:
        # CGST + SGST = GST rate split equally
        half_rate = gst_rate / 2
        cgst_amount = round_half(qty * rate * half_rate / 100, 2)
        sgst_amount = round_half(qty * rate * half_rate / 100, 2)
        igst_amount = 0.0
    
    frappe.logger().debug(
        f"Item {item.item_code}: Template={template_name}, Rate={gst_rate}%, "
        f"CGST={cgst_amount}, SGST={sgst_amount}, IGST={igst_amount}"
    )
    
    return {
        "cgst_amount": cgst_amount,
        "sgst_amount": sgst_amount,
        "igst_amount": igst_amount
    }



def check_if_inter_state(invoice_doc):
    """
    Check if invoice is inter-state (IGST) or intra-state (CGST+SGST).
    
    Args:
        invoice_doc: Sales Invoice document
        
    Returns:
        True if inter-state, False if intra-state
    """
    try:
        company_gstin = frappe.get_cached_value("Company", invoice_doc.company, "gstin")
        customer_gstin = invoice_doc.billing_address_gstin or invoice_doc.customer_gstin or ""
        
        if not company_gstin or not customer_gstin:
            # Default to inter-state if GSTIN missing
            return True
        
        # Compare first 2 digits (state code)
        company_state = company_gstin[:2]
        customer_state = customer_gstin[:2]
        
        return company_state != customer_state
        
    except Exception as e:
        frappe.logger().error(f"Error checking inter-state: {str(e)}")
        # Default to inter-state on error
        return True


# ========= DOC HOOKS =========
def on_validate(doc, method):
    if doc.doctype != "Sales Invoice":
        return
    if not doc.items or not doc.taxes:
        return

    # Early: signal all IC patches to skip
    doc.flags.skip_gst_validations = True

    if doc.docstatus == 2:
        return

    if getattr(doc, "is_return", False) and doc.return_against:
        apply_credit_note_override(doc)
    else:
        apply_normal_invoice_override(doc)


def on_before_submit(doc, method):
    if doc.doctype != "Sales Invoice":
        return

    # if doc.item_wise_tax_detail:
	if any(row.item_wise_tax_detail for row in doc.get("taxes") if row.item_wise_tax_detail):

        try:
            json.loads(doc.item_wise_tax_detail)
        except json.JSONDecodeError:
            frappe.throw(_("item_wise_tax_detail is not valid JSON"))


# ========= NORMAL INVOICE =========
def apply_normal_invoice_override(doc):
    """Override GST for non-return Sales Invoice using Item Tax Template."""
    doc.flags.skip_gst_validations = True
    doc.flags.ignore_mandatory = True

    if doc.docstatus == 2 or doc.get("is_return", False):
        return

    # STEP 1: Calculate per-item GST from Item Tax Template
    for item in doc.items:
        gst_amounts = calculate_item_gst_amounts(item, doc)
        
        item.cgst_amount = gst_amounts["cgst_amount"]
        item.sgst_amount = gst_amounts["sgst_amount"]
        item.igst_amount = gst_amounts["igst_amount"]
        qty = float(item.qty or 0)
        rate = float(item.rate or 0)

        gst_rate, is_inter_state = get_item_gst_rate(item, doc)

        if is_inter_state:
            item.igst_rate = gst_rate
            item.cgst_rate = 0
            item.sgst_rate = 0

            
        else:
            half = gst_rate / 2.0
            item.igst_rate = 0
            item.cgst_rate = half
            item.sgst_rate = half

            

    # STEP 2: Sum item-wise amounts
    total_cgst = sum(float(getattr(i, "cgst_amount", 0) or 0) for i in doc.items)
    total_sgst = sum(float(getattr(i, "sgst_amount", 0) or 0) for i in doc.items)
    total_igst = sum(float(getattr(i, "igst_amount", 0) or 0) for i in doc.items)
    total_tax_amount = total_cgst + total_sgst + total_igst

    # STEP 3: Header totals
    doc.total_taxes_and_charges = total_tax_amount
    doc.base_total_taxes_and_charges = total_tax_amount

    net = float(doc.net_total or 0)
    base_net = float(doc.base_total or 0)

    grand_total = net + total_tax_amount
    base_grand_total = base_net + total_tax_amount

    # STEP 4: Rounding
    decimal_part = base_grand_total - floor(base_grand_total)
    base_rounded_total = floor(base_grand_total) + 1 if decimal_part >= 0.5 else floor(
        base_grand_total
    )
    base_rounding_adjustment = base_rounded_total - base_grand_total

    # STEP 5: Push sums into tax rows
    # for tax in doc.taxes:
    #     tax.dont_recompute_tax = 1
    #     tt = ((tax.get("gst_tax_type") or "") or (tax.get("account_head") or "")).lower()

    #     if "cgst" in tt:
    #         tax.tax_amount = total_cgst
    #         tax.base_tax_amount = total_cgst
    #         tax.tax_amount_after_discount_amount = total_cgst
    #         tax.base_tax_amount_after_discount_amount = total_cgst

    #     elif "sgst" in tt or "utgst" in tt:
    #         tax.tax_amount = total_sgst
    #         tax.base_tax_amount = total_sgst
    #         tax.tax_amount_after_discount_amount = total_sgst
    #         tax.base_tax_amount_after_discount_amount = total_sgst

    #     elif "igst" in tt:
    #         tax.tax_amount = total_igst
    #         tax.base_tax_amount = total_igst
    #         tax.tax_amount_after_discount_amount = total_igst
    #         tax.base_tax_amount_after_discount_amount = total_igst

    # STEP 5: Push sums into tax rows AND recalculate cumulative totals
    running_total = base_net  # Start with base total (before tax)

    for tax in doc.taxes:
        tax.dont_recompute_tax = 1
        tt = ((tax.get("gst_tax_type") or "") or (tax.get("account_head") or "")).lower()

        if "cgst" in tt:
            tax.tax_amount = total_cgst
            tax.base_tax_amount = total_cgst
            tax.tax_amount_after_discount_amount = total_cgst
            tax.base_tax_amount_after_discount_amount = total_cgst
            running_total += total_cgst  # Add CGST to running total

        elif "sgst" in tt or "utgst" in tt:
            tax.tax_amount = total_sgst
            tax.base_tax_amount = total_sgst
            tax.tax_amount_after_discount_amount = total_sgst
            tax.base_tax_amount_after_discount_amount = total_sgst
            running_total += total_sgst  # Add SGST to running total

        elif "igst" in tt:
            tax.tax_amount = total_igst
            tax.base_tax_amount = total_igst
            tax.tax_amount_after_discount_amount = total_igst
            tax.base_tax_amount_after_discount_amount = total_igst
            running_total += total_igst  # Add IGST to running total

    # ✅ FIX: Update cumulative total for this tax row
        tax.total = running_total
        tax.base_total = running_total

    # STEP 6: Final totals
    doc.grand_total = grand_total
    doc.base_grand_total = base_grand_total
    doc.rounding_adjustment = base_rounding_adjustment
    doc.base_rounding_adjustment = base_rounding_adjustment
    doc.rounded_total = round_half_up(grand_total)
    doc.base_rounded_total = base_rounded_total
    doc.outstanding_amount = base_rounded_total

    # STEP 7: Block recalculation
    doc.flags.ignore_validate_update_after_submit = True
    doc.flags.dont_update_if_missing = True
    doc.flags.dont_recalculate_taxes = True

    def dummy_calculate():
        pass

    doc.calculate_taxes_and_totals = dummy_calculate

    # STEP 8: item_wise_tax_detail
    rebuild_item_wise_tax_detail_from_item_fields(doc)

    frappe.logger().debug(
        f"[GST Override] {doc.name}: "
        f"CGST={total_cgst}, SGST={total_sgst}, IGST={total_igst}, "
        f"total_taxes_and_charges={doc.total_taxes_and_charges}"
    )


# ========= CREDIT NOTE =========
def apply_credit_note_override(doc):
    """Same logic for returns using Item Tax Template."""
    doc.flags.skip_gst_validations = True
    doc.flags.ignore_mandatory = True

    if doc.docstatus == 2:
        return

    # STEP 1: Calculate per-item GST (handles negative qty automatically)
    for item in doc.items:
        gst_amounts = calculate_item_gst_amounts(item, doc)
        
        item.cgst_amount = gst_amounts["cgst_amount"]
        item.sgst_amount = gst_amounts["sgst_amount"]
        item.igst_amount = gst_amounts["igst_amount"]

    # STEP 2-8: Same logic as normal invoice
    total_cgst = sum(float(getattr(i, "cgst_amount", 0) or 0) for i in doc.items)
    total_sgst = sum(float(getattr(i, "sgst_amount", 0) or 0) for i in doc.items)
    total_igst = sum(float(getattr(i, "igst_amount", 0) or 0) for i in doc.items)
    total_tax_amount = total_cgst + total_sgst + total_igst

    doc.total_taxes_and_charges = total_tax_amount
    doc.base_total_taxes_and_charges = total_tax_amount

    net = float(doc.net_total or 0)
    base_net = float(doc.base_total or 0)

    grand_total = net + total_tax_amount
    base_grand_total = base_net + total_tax_amount

    decimal_part = base_grand_total - floor(base_grand_total)
    base_rounded_total = floor(base_grand_total) + 1 if decimal_part >= 0.5 else floor(
        base_grand_total
    )
    base_rounding_adjustment = base_rounded_total - base_grand_total

    # STEP 5: Push sums into tax rows AND recalculate cumulative totals
    running_total = base_net  # Start with base total (before tax)

    for tax in doc.taxes:
        tax.dont_recompute_tax = 1
        tt = ((tax.get("gst_tax_type") or "") or (tax.get("account_head") or "")).lower()

        if "cgst" in tt:
            tax.tax_amount = total_cgst
            tax.base_tax_amount = total_cgst
            tax.tax_amount_after_discount_amount = total_cgst
            tax.base_tax_amount_after_discount_amount = total_cgst
            running_total += total_cgst  # Add CGST to running total

        elif "sgst" in tt or "utgst" in tt:
            tax.tax_amount = total_sgst
            tax.base_tax_amount = total_sgst
            tax.tax_amount_after_discount_amount = total_sgst
            tax.base_tax_amount_after_discount_amount = total_sgst
            running_total += total_sgst  # Add SGST to running total

        elif "igst" in tt:
            tax.tax_amount = total_igst
            tax.base_tax_amount = total_igst
            tax.tax_amount_after_discount_amount = total_igst
            tax.base_tax_amount_after_discount_amount = total_igst
            running_total += total_igst  # Add IGST to running total

    # ✅ FIX: Update cumulative total for this tax row
        tax.total = running_total
        tax.base_total = running_total

    doc.grand_total = grand_total
    doc.base_grand_total = base_grand_total
    doc.rounding_adjustment = base_rounding_adjustment
    doc.base_rounding_adjustment = base_rounding_adjustment
    doc.rounded_total = round_half_up(grand_total)
    doc.base_rounded_total = base_rounded_total
    doc.outstanding_amount = base_rounded_total

    doc.flags.ignore_validate_update_after_submit = True
    doc.flags.dont_update_if_missing = True
    doc.flags.dont_recalculate_taxes = True

    def dummy_calculate():
        pass

    doc.calculate_taxes_and_totals = dummy_calculate

    rebuild_item_wise_tax_detail_from_item_fields(doc)

    frappe.logger().debug(
        f"[GST Override CN] {doc.name}: "
        f"CGST={total_cgst}, SGST={total_sgst}, IGST={total_igst}"
    )


# ========= HELPER =========
def rebuild_item_wise_tax_detail_from_item_fields(doc):
    """
    Build item_wise_tax_detail from item.cgst_amount/sgst_amount/igst_amount.
    Format: {"item_code|hsn": [net, cgst, sgst, igst, cess, total_tax]}
    """
    item_wise_tax = {}

    for item in doc.items:
        hsn = item.get("hsn_code") or item.get("gst_hsn_code") or ""
        key = f"{item.item_code}|{hsn}"

        net = float(item.net_amount or 0)
        cgst = float(getattr(item, "cgst_amount", 0) or 0)
        sgst = float(getattr(item, "sgst_amount", 0) or 0)
        igst = float(getattr(item, "igst_amount", 0) or 0)
        cess = 0.0

        total_item_tax = cgst + sgst + igst + cess
        item_wise_tax[key] = [net, cgst, sgst, igst, cess, total_item_tax]

    doc.item_wise_tax_detail = json.dumps(item_wise_tax)
    frappe.logger().debug(f"[GST Override] item_wise_tax_detail: {doc.item_wise_tax_detail}")

