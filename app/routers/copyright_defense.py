from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, EmailStr, HttpUrl
import asyncio
from datetime import datetime

from app.utils.copyright_defense import copyright_engine, takedown_generator, CopyrightViolation
from app.utils.copyright_storage import copyright_storage
from app.utils.emailing import send_email_smtp, render_email
from app.core.config import logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid

router = APIRouter(prefix="/api/copyright-defense", tags=["Copyright Defense"])

# Pydantic models
class ScanRequest(BaseModel):
    image_urls: List[HttpUrl]
    owner_email: EmailStr
    min_similarity: float = 0.7
    auto_send_takedowns: bool = True

class OwnerInfo(BaseModel):
    name: str
    email: EmailStr
    address: Optional[str] = ""
    phone: Optional[str] = ""
    company: Optional[str] = ""

class TakedownRequest(BaseModel):
    violation_id: str
    owner_info: OwnerInfo
    template_type: str = "dmca_standard"
    custom_message: Optional[str] = None

class ViolationResponse(BaseModel):
    violation_id: str
    image_url: str
    infringing_url: str
    similarity_score: float
    detected_at: datetime
    site_info: Dict[str, Any]
    status: str
    takedown_sent: bool

class ScanResponse(BaseModel):
    scan_id: str
    total_images: int
    total_violations: int
    violations: List[ViolationResponse]
    scan_completed_at: datetime

@router.post("/scan", response_model=ScanResponse)
async def scan_for_violations(
    scan_request: ScanRequest,
    background_tasks: BackgroundTasks,
    request: Request
):
    """
    Scan watermarked images for copyright violations
    """
    try:
        # Get user UID using workspace resolution
        eff_uid, req_uid = resolve_workspace_uid(request)
        if not eff_uid or not req_uid:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_uid = eff_uid
        
        # Convert HttpUrl objects to strings
        image_urls = [str(url) for url in scan_request.image_urls]
        
        logger.info(f"Starting copyright scan for {len(image_urls)} images for user {user_uid}")
        
        # Perform batch scan
        scan_results = await copyright_engine.batch_scan_images(
            image_urls, 
            str(scan_request.owner_email)
        )
        
        # Collect all violations and save to storage
        all_violations = []
        for image_url, violations in scan_results.items():
            for violation in violations:
                # Save to persistent storage
                copyright_storage.save_violation(user_uid, violation)
                all_violations.append(violation)
        
        # If auto-send is enabled, schedule takedown letters
        if scan_request.auto_send_takedowns and all_violations:
            background_tasks.add_task(
                auto_send_takedown_letters,
                user_uid,
                all_violations,
                {
                    "name": "Copyright Owner",  # Should come from user profile
                    "email": str(scan_request.owner_email),
                    "address": "",
                    "phone": ""
                }
            )
        
        # Generate scan ID
        scan_id = f"scan_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        
        # Convert violations to response format
        violation_responses = [
            ViolationResponse(
                violation_id=v.violation_id,
                image_url=v.image_url,
                infringing_url=v.infringing_url,
                similarity_score=v.similarity_score,
                detected_at=v.detected_at,
                site_info=v.site_info,
                status=v.status,
                takedown_sent=v.takedown_sent
            )
            for v in all_violations
        ]
        
        return ScanResponse(
            scan_id=scan_id,
            total_images=len(image_urls),
            total_violations=len(all_violations),
            violations=violation_responses,
            scan_completed_at=datetime.utcnow()
        )
        
    except Exception as e:
        logger.error(f"Error in copyright scan: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")

@router.post("/send-takedown")
async def send_takedown_letter(
    takedown_request: TakedownRequest,
    request: Request
):
    """
    Generate and send a DMCA takedown letter for a specific violation
    """
    try:
        # Get user UID using workspace resolution
        eff_uid, req_uid = resolve_workspace_uid(request)
        if not eff_uid or not req_uid:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_uid = eff_uid
        
        # Get violation details from storage
        violation = copyright_storage.get_violation(user_uid, takedown_request.violation_id)
        if not violation:
            raise HTTPException(status_code=404, detail="Violation not found")
        
        # Generate takedown letter
        letter_data = takedown_generator.generate_takedown_letter(
            violation,
            takedown_request.owner_info.dict(),
            takedown_request.template_type
        )
        
        # Determine recipient email
        recipient_email = await get_contact_email_for_domain(letter_data["recipient_domain"])
        
        if not recipient_email:
            raise HTTPException(
                status_code=400, 
                detail=f"Could not determine contact email for {letter_data['recipient_domain']}"
            )
        
        # Add custom message if provided
        html_content = letter_data["html_content"]
        text_content = letter_data["text_content"]
        if request.custom_message:
            # For HTML, add custom message at the top
            custom_html = f"<div style='background:#fff3cd;border:1px solid #ffeaa7;padding:15px;margin:20px 0;border-radius:5px;'><strong>Additional Message:</strong><br>{request.custom_message.replace(chr(10), '<br>')}</div>"
            html_content = html_content.replace('<div class="content">', f'<div class="content">{custom_html}')
            # For text, add at the beginning
            text_content = f"ADDITIONAL MESSAGE:\n{request.custom_message}\n\n{text_content}"
        
        # Send email
        success = send_email_smtp(
            to_addr=recipient_email,
            subject=letter_data["subject"],
            html=html_content,
            text=text_content,
            from_addr=str(takedown_request.owner_info.email),
            from_name=takedown_request.owner_info.name
        )
        
        if success:
            # Update violation status in storage
            copyright_storage.update_violation_status(
                user_uid,
                takedown_request.violation_id, 
                "takedown_sent",
                {"recipient_email": recipient_email}
            )
            
            return {
                "success": True,
                "message": f"Takedown letter sent to {recipient_email}",
                "violation_id": request.violation_id,
                "recipient": recipient_email
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to send takedown letter")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending takedown letter: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to send takedown: {str(e)}")

@router.get("/violations/{violation_id}")
async def get_violation_details(
    violation_id: str,
    request: Request
):
    """
    Get details of a specific violation
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_uid = eff_uid
    
    violation = copyright_storage.get_violation(user_uid, violation_id)
    if not violation:
        raise HTTPException(status_code=404, detail="Violation not found")
    
    return violation.to_dict()

@router.put("/violations/{violation_id}/status")
async def update_violation_status(
    violation_id: str,
    status: str,
    request: Request,
    notes: Optional[str] = None
):
    """
    Update the status of a violation (e.g., mark as resolved, ignored)
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_uid = eff_uid
    
    valid_statuses = ["detected", "takedown_sent", "resolved", "ignored"]
    if status not in valid_statuses:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
        )
    
    additional_data = {}
    if notes:
        additional_data["notes"] = notes
    
    success = copyright_storage.update_violation_status(user_uid, violation_id, status, additional_data)
    
    if not success:
        raise HTTPException(status_code=404, detail="Violation not found")
    
    return {"success": True, "violation_id": violation_id, "new_status": status}

@router.post("/preview-takedown")
async def preview_takedown_letter(
    takedown_request: TakedownRequest,
    request: Request
):
    """
    Preview a takedown letter without sending it
    """
    try:
        eff_uid, req_uid = resolve_workspace_uid(request)
        if not eff_uid or not req_uid:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_uid = eff_uid
        
        violation = copyright_storage.get_violation(user_uid, takedown_request.violation_id)
        if not violation:
            raise HTTPException(status_code=404, detail="Violation not found")
        
        letter_data = takedown_generator.generate_takedown_letter(
            violation,
            takedown_request.owner_info.dict(),
            takedown_request.template_type
        )
        
        # Add custom message if provided
        html_content = letter_data["html_content"]
        text_content = letter_data["text_content"]
        if takedown_request.custom_message:
            # For HTML, add custom message at the top
            custom_html = f"<div style='background:#fff3cd;border:1px solid #ffeaa7;padding:15px;margin:20px 0;border-radius:5px;'><strong>Additional Message:</strong><br>{takedown_request.custom_message.replace(chr(10), '<br>')}</div>"
            html_content = html_content.replace('<div class="content">', f'<div class="content">{custom_html}')
            # For text, add at the beginning
            text_content = f"ADDITIONAL MESSAGE:\n{takedown_request.custom_message}\n\n{text_content}"
        
        return {
            "subject": letter_data["subject"],
            "html_content": html_content,
            "text_content": text_content,
            "recipient_domain": letter_data["recipient_domain"],
            "template_type": letter_data["template_type"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error previewing takedown letter: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Preview failed: {str(e)}")

@router.get("/stats")
async def get_copyright_stats(
    request: Request
):
    """
    Get copyright defense statistics
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_uid = eff_uid
    
    stats = copyright_storage.get_user_stats(user_uid)
    return stats

@router.get("/violations")
async def list_violations(
    request: Request,
    limit: int = 50,
    status: Optional[str] = None
):
    """
    List all violations for the current user
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_uid = eff_uid
    
    violations = copyright_storage.get_user_violations(user_uid, limit)
    
    # Filter by status if provided
    if status:
        violations = [v for v in violations if v.status == status]
    
    return {
        "violations": [v.to_dict() for v in violations],
        "total": len(violations)
    }

@router.delete("/violations/{violation_id}")
async def delete_violation(
    violation_id: str,
    request: Request
):
    """
    Delete a violation
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_uid = eff_uid
    
    success = copyright_storage.delete_violation(user_uid, violation_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Violation not found")
    
    return {"success": True, "violation_id": violation_id}

# Background task functions
async def auto_send_takedown_letters(user_uid: str, violations: List[CopyrightViolation], owner_info: Dict[str, str]):
    """
    Background task to automatically send takedown letters
    """
    logger.info(f"Auto-sending takedown letters for {len(violations)} violations")
    
    for violation in violations:
        try:
            # Generate letter
            letter_data = takedown_generator.generate_takedown_letter(
                violation,
                owner_info,
                "dmca_standard"
            )
            
            # Get contact email
            recipient_email = await get_contact_email_for_domain(letter_data["recipient_domain"])
            
            if recipient_email:
                # Send email
                success = send_email_smtp(
                    to_addr=recipient_email,
                    subject=letter_data["subject"],
                    html=letter_data["html_content"],
                    text=letter_data["text_content"],
                    from_addr=owner_info["email"],
                    from_name=owner_info["name"]
                )
                
                if success:
                    copyright_storage.update_violation_status(
                        user_uid,
                        violation.violation_id,
                        "takedown_sent",
                        {"recipient_email": recipient_email, "auto_sent": True}
                    )
                    logger.info(f"Auto-sent takedown for violation {violation.violation_id}")
                else:
                    logger.error(f"Failed to auto-send takedown for violation {violation.violation_id}")
            
            # Rate limiting
            await asyncio.sleep(5)  # 5 seconds between emails
            
        except Exception as e:
            logger.error(f"Error auto-sending takedown for violation {violation.violation_id}: {str(e)}")

async def get_contact_email_for_domain(domain: str) -> Optional[str]:
    """
    Attempt to find contact email for a domain
    This is a simplified version - in production, you might want to:
    1. Check WHOIS data
    2. Look for contact pages
    3. Use common patterns (abuse@, dmca@, legal@, etc.)
    """
    common_contacts = [
        f"dmca@{domain}",
        f"legal@{domain}",
        f"abuse@{domain}",
        f"copyright@{domain}",
        f"contact@{domain}",
        f"admin@{domain}"
    ]
    
    # For now, return the first common contact
    # In production, you'd want to verify these emails exist
    return common_contacts[0]