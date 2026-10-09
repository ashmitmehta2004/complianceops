"""Vendor endpoints: the live deterministic evaluation plus persisted evidence, and creation."""

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import SessionFactoryDep
from app.api.errors import ApiError
from app.api.schemas import CreateVendorRequest, ErrorView, VendorDetailView, VendorSummaryView
from app.api.views import vendor_detail, vendor_summary
from app.persistence.models import Vendor, utcnow

router = APIRouter(prefix="/vendors", tags=["vendors"])


@router.get("", response_model=list[VendorSummaryView], summary="List vendors with status")
def list_vendors(factory: SessionFactoryDep) -> list[VendorSummaryView]:
    now = utcnow()
    with factory() as session:
        vendors = session.scalars(select(Vendor).order_by(Vendor.name))
        return [vendor_summary(session, v, now) for v in vendors]


@router.get(
    "/{vendor_id}",
    response_model=VendorDetailView,
    responses={404: {"model": ErrorView}},
    summary="Vendor details: requirements, evidence, reviews, approvals and runs",
)
def get_vendor(vendor_id: str, factory: SessionFactoryDep) -> VendorDetailView:
    with factory() as session:
        vendor = session.get(Vendor, vendor_id)
        if vendor is None:
            raise ApiError(404, "vendor_not_found", "vendor not found")
        return vendor_detail(session, vendor)


@router.post(
    "",
    status_code=201,
    response_model=VendorDetailView,
    responses={409: {"model": ErrorView}, 422: {"model": ErrorView}},
    summary="Create a vendor (no evidence, review or approval is created)",
)
def create_vendor(body: CreateVendorRequest, factory: SessionFactoryDep) -> VendorDetailView:
    """A new vendor has no evidence, so the policy engine reports every requirement MISSING.
    A review is created later, by the first run, through the existing workflow."""
    duplicate = ApiError(409, "vendor_exists", f"a vendor named '{body.name}' already exists")
    metadata = {"description": body.description} if body.description else {}
    with factory() as session:
        # Case-insensitive, matching how runs resolve vendors by name.
        if session.scalars(
            select(Vendor.id).where(func.lower(Vendor.name) == body.name.lower())
        ).first():
            raise duplicate
        vendor = Vendor(name=body.name, metadata_=metadata)
        session.add(vendor)
        try:
            session.commit()  # the unique constraint also covers a concurrent create
        except IntegrityError:
            session.rollback()
            raise duplicate from None
        return vendor_detail(session, vendor)
