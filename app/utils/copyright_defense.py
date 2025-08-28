import httpx
import asyncio
import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import json
import hashlib
from urllib.parse import urlparse
from app.core.config import logger
from app.utils.emailing import render_email

# RapidAPI configuration for Google Image Lens Search
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "3e05afefd5mshce80f9e6147873ep1538b6jsn40bd0d052343")
RAPIDAPI_HOST = "google-image-lens-search.p.rapidapi.com"
RAPIDAPI_URL = f"https://{RAPIDAPI_HOST}/getLinks"

class CopyrightViolation:
    """Represents a detected copyright violation"""
    def __init__(self, image_url: str, infringing_url: str, similarity_score: float = 0.0, 
                 detected_at: datetime = None, site_info: Dict = None):
        self.image_url = image_url
        self.infringing_url = infringing_url
        self.similarity_score = similarity_score
        self.detected_at = detected_at or datetime.utcnow()
        self.site_info = site_info or {}
        self.violation_id = self._generate_violation_id()
        self.takedown_sent = False
        self.takedown_sent_at = None
        self.response_received = False
        self.response_received_at = None
        self.status = "detected"  # detected, takedown_sent, resolved, ignored
    
    def _generate_violation_id(self) -> str:
        """Generate unique violation ID"""
        content = f"{self.image_url}_{self.infringing_url}_{self.detected_at.isoformat()}"
        return hashlib.md5(content.encode()).hexdigest()[:12]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "violation_id": self.violation_id,
            "image_url": self.image_url,
            "infringing_url": self.infringing_url,
            "similarity_score": self.similarity_score,
            "detected_at": self.detected_at.isoformat(),
            "site_info": self.site_info,
            "takedown_sent": self.takedown_sent,
            "takedown_sent_at": self.takedown_sent_at.isoformat() if self.takedown_sent_at else None,
            "response_received": self.response_received,
            "response_received_at": self.response_received_at.isoformat() if self.response_received_at else None,
            "status": self.status
        }

class CopyrightDefenseEngine:
    """Main engine for copyright defense operations"""
    
    def __init__(self):
        self.violations_cache = {}  # In-memory cache for current session
        self.rate_limit_delay = 2  # seconds between API calls
        
    async def reverse_image_search(self, image_url: str) -> List[Dict[str, Any]]:
        """
        Perform reverse image search using Google Image Lens Search API
        """
        try:
            headers = {
                'Content-Type': 'application/json',
                'x-rapidapi-host': RAPIDAPI_HOST,
                'x-rapidapi-key': RAPIDAPI_KEY
            }
            
            payload = {
                "image_url": image_url
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(RAPIDAPI_URL, headers=headers, json=payload)
                
                if response.status_code == 200:
                    data = response.json()
                    return self._parse_search_results(data)
                else:
                    logger.error(f"Reverse image search failed: {response.status_code} - {response.text}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error in reverse image search: {str(e)}")
            return []
    
    def _parse_search_results(self, api_response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse API response and extract relevant information"""
        results = []
        
        # The API response structure may vary, adjust based on actual response format
        if "links" in api_response:
            for link_data in api_response["links"]:
                if isinstance(link_data, dict):
                    result = {
                        "url": link_data.get("url", ""),
                        "title": link_data.get("title", ""),
                        "source": link_data.get("source", ""),
                        "thumbnail": link_data.get("thumbnail", ""),
                        "similarity_score": link_data.get("similarity", 0.0)
                    }
                    results.append(result)
        
        return results
    
    async def scan_for_violations(self, watermarked_image_url: str, 
                                 owner_email: str, 
                                 min_similarity: float = 0.7) -> List[CopyrightViolation]:
        """
        Scan for copyright violations of a watermarked image
        """
        logger.info(f"Scanning for violations of image: {watermarked_image_url}")
        
        # Perform reverse image search
        search_results = await self.reverse_image_search(watermarked_image_url)
        
        violations = []
        for result in search_results:
            # Filter out results with low similarity
            if result.get("similarity_score", 0) < min_similarity:
                continue
                
            # Skip if it's the original source or known legitimate sites
            if self._is_legitimate_source(result["url"], owner_email):
                continue
            
            # Create violation record
            violation = CopyrightViolation(
                image_url=watermarked_image_url,
                infringing_url=result["url"],
                similarity_score=result.get("similarity_score", 0.0),
                site_info={
                    "title": result.get("title", ""),
                    "source": result.get("source", ""),
                    "thumbnail": result.get("thumbnail", "")
                }
            )
            
            violations.append(violation)
            self.violations_cache[violation.violation_id] = violation
            
            # Rate limiting
            await asyncio.sleep(self.rate_limit_delay)
        
        logger.info(f"Found {len(violations)} potential violations")
        return violations
    
    def _is_legitimate_source(self, url: str, owner_email: str) -> bool:
        """
        Check if a URL is a legitimate source (owner's website, stock photo sites, etc.)
        """
        domain = urlparse(url).netloc.lower()
        
        # Skip common legitimate sources
        legitimate_domains = [
            'photomark.cloud',  # Your own service
            'unsplash.com',
            'pexels.com',
            'pixabay.com',
            'shutterstock.com',
            'getty.com',
            'adobe.com',
            'istockphoto.com'
        ]
        
        for legit_domain in legitimate_domains:
            if legit_domain in domain:
                return True
        
        # TODO: Add logic to check if domain belongs to the owner
        # This could involve checking WHOIS data or a user-maintained whitelist
        
        return False
    
    async def batch_scan_images(self, image_urls: List[str], 
                               owner_email: str) -> Dict[str, List[CopyrightViolation]]:
        """
        Scan multiple images for violations
        """
        results = {}
        
        for image_url in image_urls:
            try:
                violations = await self.scan_for_violations(image_url, owner_email)
                results[image_url] = violations
                
                # Rate limiting between images
                await asyncio.sleep(self.rate_limit_delay)
                
            except Exception as e:
                logger.error(f"Error scanning {image_url}: {str(e)}")
                results[image_url] = []
        
        return results
    
    def get_violation_by_id(self, violation_id: str) -> Optional[CopyrightViolation]:
        """Get violation by ID"""
        return self.violations_cache.get(violation_id)
    
    def update_violation_status(self, violation_id: str, status: str, 
                              additional_data: Dict = None) -> bool:
        """Update violation status"""
        if violation_id in self.violations_cache:
            violation = self.violations_cache[violation_id]
            violation.status = status
            
            if status == "takedown_sent":
                violation.takedown_sent = True
                violation.takedown_sent_at = datetime.utcnow()
            elif status == "resolved":
                violation.response_received = True
                violation.response_received_at = datetime.utcnow()
            
            if additional_data:
                violation.site_info.update(additional_data)
            
            return True
        return False

class TakedownLetterGenerator:
    """Generate DMCA takedown letters"""
    
    def __init__(self):
        self.templates = {
            "dmca_standard": self._get_dmca_template(),
            "dmca_social_media": self._get_social_media_template(),
            "dmca_hosting": self._get_hosting_template()
        }
    
    def generate_takedown_letter(self, violation: CopyrightViolation, 
                                owner_info: Dict[str, str],
                                template_type: str = "dmca_standard") -> Dict[str, str]:
        """
        Generate a DMCA takedown letter
        """
        # Extract domain info
        parsed_url = urlparse(violation.infringing_url)
        domain = parsed_url.netloc
        
        # Prepare template variables
        template_vars = {
            "owner_name": owner_info.get("name", "Copyright Owner"),
            "owner_email": owner_info.get("email", ""),
            "owner_address": owner_info.get("address", ""),
            "owner_phone": owner_info.get("phone", ""),
            "owner_company": owner_info.get("company", ""),
            "original_work_url": violation.image_url,
            "infringing_url": violation.infringing_url,
            "infringing_domain": domain,
            "violation_id": violation.violation_id,
            "date": datetime.utcnow().strftime("%B %d, %Y"),
            "site_title": violation.site_info.get("title", "Untitled Page"),
            "similarity_score": int(violation.similarity_score * 100) if violation.similarity_score else 85
        }
        
        # Generate HTML content using the email template
        html_content = render_email("takedown_notice.html", **template_vars)
        
        # Generate plain text version
        text_template = self.templates.get(template_type, self.templates["dmca_standard"])
        text_content = text_template.format(**template_vars)
        
        # Generate subject line
        subject = f"🚨 DMCA Takedown Notice - Copyright Infringement on {domain}"
        
        return {
            "subject": subject,
            "html_content": html_content,
            "text_content": text_content,
            "template_type": template_type,
            "recipient_domain": domain
        }
    
    def _get_dmca_template(self) -> str:
        return """
Subject: DMCA Takedown Notice - Copyright Infringement

Dear Sir/Madam,

I am writing to notify you of copyright infringement occurring on your website. I am the copyright owner of the work described below, and I have not authorized the use of this work on your site.

IDENTIFICATION OF COPYRIGHTED WORK:
Original Work URL: {original_work_url}
Copyright Owner: {owner_name}
Contact Email: {owner_email}
Contact Address: {owner_address}
Contact Phone: {owner_phone}

IDENTIFICATION OF INFRINGING MATERIAL:
Infringing URL: {infringing_url}
Page Title: {site_title}
Violation ID: {violation_id}

STATEMENT OF GOOD FAITH BELIEF:
I have a good faith belief that the use of the copyrighted material described above is not authorized by the copyright owner, its agent, or the law.

STATEMENT OF ACCURACY:
I swear, under penalty of perjury, that the information in this notification is accurate and that I am the copyright owner or am authorized to act on behalf of the copyright owner.

REQUESTED ACTION:
I request that you immediately remove or disable access to the infringing material identified above.

Please confirm receipt of this notice and advise of the action taken within 48 hours.

Sincerely,
{owner_name}
{owner_email}
Date: {date}

---
This notice is sent in accordance with the Digital Millennium Copyright Act (DMCA), 17 U.S.C. § 512.
"""
    
    def _get_social_media_template(self) -> str:
        return """
Subject: Copyright Infringement Report - Immediate Action Required

Dear {infringing_domain} Support Team,

I am reporting copyright infringement of my original work on your platform.

ORIGINAL WORK:
- URL: {original_work_url}
- Owner: {owner_name}
- Contact: {owner_email}

INFRINGING CONTENT:
- URL: {infringing_url}
- Posted without permission
- Violation ID: {violation_id}

I have a good faith belief that this use is not authorized by me, my agent, or the law. I swear under penalty of perjury that this information is accurate and that I am the copyright owner.

Please remove this content immediately and confirm the action taken.

Best regards,
{owner_name}
{owner_email}
{date}
"""
    
    def _get_hosting_template(self) -> str:
        return """
Subject: DMCA Takedown Notice - Hosting Provider Action Required

Dear Hosting Provider,

I am submitting this DMCA takedown notice regarding copyright infringement on a website you host.

COPYRIGHT OWNER INFORMATION:
Name: {owner_name}
Email: {owner_email}
Address: {owner_address}
Phone: {owner_phone}

ORIGINAL COPYRIGHTED WORK:
{original_work_url}

INFRINGING MATERIAL:
Website: {infringing_domain}
Specific URL: {infringing_url}
Violation Reference: {violation_id}

I have a good faith belief that the use of this material is not authorized by the copyright owner, its agent, or the law. Under penalty of perjury, I certify that this information is accurate and that I am authorized to act on behalf of the copyright owner.

As the hosting provider, you are required under the DMCA to expeditiously remove or disable access to this infringing material upon receipt of this notice.

Please confirm receipt and action taken within 24 hours.

Regards,
{owner_name}
Date: {date}
"""

# Global instance
copyright_engine = CopyrightDefenseEngine()
takedown_generator = TakedownLetterGenerator()