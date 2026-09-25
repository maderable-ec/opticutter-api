"""GET /orders/{id}/pieces/export: the cut list for the commercial cutting program."""

import csv
import io
import xml.etree.ElementTree as ET

from src.modules.branches.model import BranchModel
from tests.order_helpers import _order_with_banding, _token_for


def _export(client, order_id, fmt, headers=None):
    return client.get(
        f"/api/v1/orders/{order_id}/pieces/export",
        params={"format": fmt},
        headers=headers,
    )


def test_the_xml_is_the_cut_list_with_its_client(client, db_session):
    order = _order_with_banding(client, db_session)

    resp = _export(client, order["id"], "xml")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/xml"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'filename="piezas_{order["code"]}.xml"' in disposition
    row = ET.fromstring(resp.content).find("parts/row")
    fields = {child.tag: child.text for child in row}
    assert fields["length"] == "500"
    assert fields["width"] == "1000"
    assert fields["quantity"] == "1"
    assert fields["allow_rotation"] == "1"
    assert fields["label"] == "2C CS Costado"
    assert fields["material"] == order["pieces"][0]["productName"]
    assert fields["customer"] == "Ada Lovelace"


def test_the_csv_is_the_web_imports_layout(client, db_session):
    order = _order_with_banding(client, db_session)

    resp = _export(client, order["id"], "csv")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        f'filename="piezas_{order["code"]}.csv"' in resp.headers["content-disposition"]
    )
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows[0] == [
        "Material",
        "Largo",
        "Ancho",
        "Cantidad",
        "Prioridad",
        "Etiqueta",
        "Rotar",
    ]
    assert rows[1:] == [
        [
            order["pieces"][0]["productName"],
            "500",
            "1000",
            "1",
            "",
            "2C CS Costado",
            "sí",
        ]
    ]


def test_an_unknown_format_is_refused(client, db_session):
    order = _order_with_banding(client, db_session)
    assert _export(client, order["id"], "pdf").status_code == 422
    assert _export(client, order["id"], "xlsx").status_code == 422


def test_the_export_is_branch_scoped(client, db_session):
    order = _order_with_banding(client, db_session)
    db_session.add(BranchModel(code="SUCX", name="Sucursal Export", is_active=True))
    db_session.commit()
    branch2 = db_session.query(BranchModel).filter(BranchModel.code == "SUCX").one()

    other = _token_for(
        client, db_session, "operador", branch_id=branch2.id, email="opx@empresa.com"
    )
    same = _token_for(client, db_session, "operador")

    assert _export(client, order["id"], "xml", headers=other).status_code == 404
    assert _export(client, order["id"], "xml", headers=same).status_code == 200
