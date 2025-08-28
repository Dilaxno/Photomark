import json
import os
from typing import Dict, List, Optional
from datetime import datetime
from app.utils.copyright_defense import CopyrightViolation
from app.core.config import logger
from app.utils.storage import write_json_key, read_json_key

class CopyrightViolationStorage:
    """Simple JSON-based storage for copyright violations"""
    
    def __init__(self):
        self.base_path = "copyright_violations"
    
    def _get_user_violations_key(self, user_uid: str) -> str:
        """Get storage key for user's violations"""
        return f"{self.base_path}/{user_uid}/violations.json"
    
    def _get_violation_key(self, user_uid: str, violation_id: str) -> str:
        """Get storage key for specific violation"""
        return f"{self.base_path}/{user_uid}/violations/{violation_id}.json"
    
    def save_violation(self, user_uid: str, violation: CopyrightViolation) -> bool:
        """Save a violation to storage"""
        try:
            violation_data = violation.to_dict()
            
            # Save individual violation
            violation_key = self._get_violation_key(user_uid, violation.violation_id)
            write_json_key(violation_key, violation_data)
            
            # Update violations index
            violations_key = self._get_user_violations_key(user_uid)
            violations_index = read_json_key(violations_key) or {}
            
            if "violations" not in violations_index:
                violations_index["violations"] = {}
            
            violations_index["violations"][violation.violation_id] = {
                "violation_id": violation.violation_id,
                "image_url": violation.image_url,
                "infringing_url": violation.infringing_url,
                "status": violation.status,
                "detected_at": violation.detected_at.isoformat(),
                "takedown_sent": violation.takedown_sent
            }
            
            violations_index["last_updated"] = datetime.utcnow().isoformat()
            write_json_key(violations_key, violations_index)
            
            return True
            
        except Exception as e:
            logger.error(f"Error saving violation {violation.violation_id}: {str(e)}")
            return False
    
    def get_violation(self, user_uid: str, violation_id: str) -> Optional[CopyrightViolation]:
        """Get a specific violation"""
        try:
            violation_key = self._get_violation_key(user_uid, violation_id)
            violation_data = read_json_key(violation_key)
            
            if not violation_data:
                return None
            
            # Reconstruct CopyrightViolation object
            violation = CopyrightViolation(
                image_url=violation_data["image_url"],
                infringing_url=violation_data["infringing_url"],
                similarity_score=violation_data.get("similarity_score", 0.0),
                detected_at=datetime.fromisoformat(violation_data["detected_at"]),
                site_info=violation_data.get("site_info", {})
            )
            
            # Update status fields
            violation.violation_id = violation_data["violation_id"]
            violation.status = violation_data.get("status", "detected")
            violation.takedown_sent = violation_data.get("takedown_sent", False)
            
            if violation_data.get("takedown_sent_at"):
                violation.takedown_sent_at = datetime.fromisoformat(violation_data["takedown_sent_at"])
            
            if violation_data.get("response_received_at"):
                violation.response_received_at = datetime.fromisoformat(violation_data["response_received_at"])
                violation.response_received = True
            
            return violation
            
        except Exception as e:
            logger.error(f"Error loading violation {violation_id}: {str(e)}")
            return None
    
    def get_user_violations(self, user_uid: str, limit: int = 100) -> List[CopyrightViolation]:
        """Get all violations for a user"""
        try:
            violations_key = self._get_user_violations_key(user_uid)
            violations_index = read_json_key(violations_key) or {}
            
            if "violations" not in violations_index:
                return []
            
            violations = []
            violation_ids = list(violations_index["violations"].keys())
            
            # Sort by detected_at descending (most recent first)
            violation_ids.sort(
                key=lambda vid: violations_index["violations"][vid].get("detected_at", ""),
                reverse=True
            )
            
            # Limit results
            violation_ids = violation_ids[:limit]
            
            for violation_id in violation_ids:
                violation = self.get_violation(user_uid, violation_id)
                if violation:
                    violations.append(violation)
            
            return violations
            
        except Exception as e:
            logger.error(f"Error loading violations for user {user_uid}: {str(e)}")
            return []
    
    def update_violation_status(self, user_uid: str, violation_id: str, 
                               status: str, additional_data: Dict = None) -> bool:
        """Update violation status"""
        try:
            violation = self.get_violation(user_uid, violation_id)
            if not violation:
                return False
            
            # Update status
            violation.status = status
            
            if status == "takedown_sent":
                violation.takedown_sent = True
                violation.takedown_sent_at = datetime.utcnow()
            elif status == "resolved":
                violation.response_received = True
                violation.response_received_at = datetime.utcnow()
            
            # Add additional data to site_info
            if additional_data:
                violation.site_info.update(additional_data)
            
            # Save updated violation
            return self.save_violation(user_uid, violation)
            
        except Exception as e:
            logger.error(f"Error updating violation {violation_id}: {str(e)}")
            return False
    
    def get_user_stats(self, user_uid: str) -> Dict:
        """Get copyright defense statistics for a user"""
        try:
            violations = self.get_user_violations(user_uid)
            
            stats = {
                "total_violations": len(violations),
                "takedowns_sent": len([v for v in violations if v.takedown_sent]),
                "resolved_cases": len([v for v in violations if v.status == "resolved"]),
                "pending_cases": len([v for v in violations if v.status == "detected"]),
                "ignored_cases": len([v for v in violations if v.status == "ignored"]),
                "recent_violations": [
                    {
                        "violation_id": v.violation_id,
                        "infringing_url": v.infringing_url,
                        "detected_at": v.detected_at.isoformat(),
                        "status": v.status
                    }
                    for v in violations[:10]  # Most recent 10
                ]
            }
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting stats for user {user_uid}: {str(e)}")
            return {
                "total_violations": 0,
                "takedowns_sent": 0,
                "resolved_cases": 0,
                "pending_cases": 0,
                "ignored_cases": 0,
                "recent_violations": []
            }
    
    def delete_violation(self, user_uid: str, violation_id: str) -> bool:
        """Delete a violation"""
        try:
            # Remove from individual storage
            violation_key = self._get_violation_key(user_uid, violation_id)
            # Note: The storage system doesn't seem to have a delete function
            # For now, we'll mark it as deleted by updating status
            violation = self.get_violation(user_uid, violation_id)
            if violation:
                violation.status = "deleted"
                return self.save_violation(user_uid, violation)
            
            return False
            
        except Exception as e:
            logger.error(f"Error deleting violation {violation_id}: {str(e)}")
            return False

# Global instance
copyright_storage = CopyrightViolationStorage()