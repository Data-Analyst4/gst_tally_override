import frappe
from frappe.tests.utils import FrappeTestCase

class TestGSTOverride(FrappeTestCase):
    def setUp(self):
        self.company = self.ensure_company()
        self.customer_inter = self.ensure_customer("GST Test Customer Inter", "27AAACT0000A1Z5")
        self.customer_intra = self.ensure_customer("GST Test Customer Intra", "07AAACT0000A1Z5")
        self.item_18 = self.ensure_item_with_template("GST-ITEM-18", "GST-18%-TEST", 18.0)

    def ensure_company(self):
        if frappe.db.exists("Company", "K95 Foods Private Limited"):
            return "K95 Foods Private Limited"
        doc = frappe.get_doc({
            "doctype": "Company",
            "company_name": "K95 Foods Private Limited",
            "abbr": "K95",
            "default_currency": "INR",
            "country": "India",
            "gstin": "07AAACK9500A1Z5",
        })
        doc.insert()
        return doc.name

    def ensure_customer(self, name, gstin):
        if frappe.db.exists("Customer", name):
            return name

        # use valid demo GSTINs
        if "Inter" in name:
            gstin = "27AAEPM0123C1Z5"  # Maharashtra, valid check digit
        else:
            gstin = "07AAEPM0123C1Z1"  # Delhi, valid check digit

        doc = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": name,
            "customer_type": "Company",
            "customer_group": "Commercial",
            "territory": "India",
            "gstin": gstin,
        })
        doc.insert()
        return doc.name


    def ensure_item_with_template(self, item_code, template_name, gst_rate):
        if not frappe.db.exists("Item Tax Template", template_name):
            tpl = frappe.get_doc({
                "doctype": "Item Tax Template",
                "title": template_name,
                "company": self.company,
                "gst_rate": gst_rate,
            })
            tpl.insert()
        if frappe.db.exists("Item", item_code):
            return item_code
        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": item_code,
            "item_name": item_code,
            "item_group": "Products",
            "stock_uom": "Nos",
        })
        item.append("taxes", {
            "item_tax_template": template_name,
            "valid_from": "2025-01-01",
        })
        item.insert()
        return item.name

    def make_invoice(self, customer, is_return=False):
        inv = frappe.get_doc({
            "doctype": "Sales Invoice",
            "company": self.company,
            "customer": customer,
            "is_return": is_return,
            "posting_date": "2025-12-04",
        })
        inv.append("items", {
            "item_code": self.item_18,
            "qty": 1,
            "rate": 1000,
        })
        inv.append("taxes", {
            "charge_type": "On Net Total",
            "account_head": "Output CGST - K95",
            "gst_tax_type": "cgst",
            "rate": 0,
        })
        inv.append("taxes", {
            "charge_type": "On Net Total",
            "account_head": "Output SGST - K95",
            "gst_tax_type": "sgst",
            "rate": 0,
        })
        inv.insert()
        inv.reload()
        return inv

    def test_intra_state_cgst_sgst(self):
        inv = self.make_invoice(self.customer_intra)
        inv.run_method("validate")
        inv.reload()

        cgst = sum(t.tax_amount for t in inv.taxes
                   if "cgst" in (t.gst_tax_type or t.account_head or "").lower())
        sgst = sum(t.tax_amount for t in inv.taxes
                   if "sgst" in (t.gst_tax_type or t.account_head or "").lower())

        self.assertEqual(cgst, 90.0)
        self.assertEqual(sgst, 90.0)
        self.assertEqual(float(inv.total_taxes_and_charges or 0), 180.0)
        self.assertEqual(float(inv.grand_total or 0), 1180.0)
        self.assertEqual(inv.rounded_total, 1180)

    def test_inter_state_igst(self):
        inv = self.make_invoice(self.customer_intra)
        inv.customer_gstin = "27AAACT0000A1Z5"
        inv.save()
        inv.reload()

        inv.run_method("validate")
        inv.reload()

        self.assertEqual(float(inv.total_taxes_and_charges or 0), 180.0)
        self.assertEqual(float(inv.grand_total or 0), 1180.0)
