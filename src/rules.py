from __future__ import annotations
from datetime import datetime, timezone
from .domain import ConflictError, ValidationError
TITLE='桥梁结构监测与限行决策'; ENTITY='桥梁告警'; ID_PREFIX='BM'
SEVERITIES=['normal', 'watch', 'warning', 'critical']; STATES=['normal', 'warning', 'restricted', 'closed', 'restored']; TRANSITIONS={'normal': ['warning'], 'warning': ['restricted'], 'restricted': ['closed'], 'closed': ['restored'], 'restored': []}; TRANSITION_ROLES={'warning': ['sensor_operator'], 'restricted': ['bridge_engineer'], 'closed': ['traffic_authority'], 'restored': ['bridge_engineer']}
CREATE_ROLES=set(['sensor_operator']); RECORD_ROLES=set(['sensor_operator', 'bridge_engineer']); AUDIT_ROLES=set(['bridge_engineer', 'viewer']); VIEW_ROLES=set(['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer'])
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'warning': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'warning': 8, 'critical': 4}; TERMINAL_STATES=set(['restored'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# ===== 斜拉索索力缓变识别 =====
READING_ACCEPTED='accepted'; READING_PENDING='pending'
READING_STATUSES=[READING_ACCEPTED, READING_PENDING]
PENDING_OFFLINE='offline'; PENDING_MISSING_TEMPERATURE='missing_temperature'
PENDING_TIME_INVERSION='time_inversion'; PENDING_BASELINE_MISSING='baseline_missing'
PENDING_REASONS=[PENDING_OFFLINE, PENDING_MISSING_TEMPERATURE,
                 PENDING_TIME_INVERSION, PENDING_BASELINE_MISSING]
CONSECUTIVE_LIMIT=3
DEFAULT_BAND_WIDTH=2.0
TRAFFIC_NOTICE_KIND='traffic_notice'
ALARM_RECORD_KIND='severe_alarm'

def parse_measured_at(value):
    if not isinstance(value,str) or not value.strip(): raise ValidationError("measured_at不能为空")
    text=value.strip()
    try:
        if text.endswith('Z'): text=text[:-1]+'+00:00'
        parsed=datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("measured_at必须是ISO 8601时间") from exc
    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def find_baseline(baselines,temperature):
    # 左闭右开：tmin <= temperature < tmax；无上界时 tmax 为 None 或 tmin 兜底
    for base in baselines:
        tmin=float(base['tmin']); tmax=base.get('tmax')
        if temperature>=tmin and (tmax is None or temperature<float(tmax)):
            return base
    return None

def adjust_force(force,temperature,baseline):
    adjusted=float(force)-float(baseline['baseline_force'])
    return {"temperature":float(temperature),"baseline_force":float(baseline['baseline_force']),
            "band_tmin":float(baseline['tmin']),"band_tmax":(None if baseline.get('tmax') is None else float(baseline['tmax'])),
            "baseline_id":baseline['id'],"force_adjusted":adjusted}

def is_over_limit(force_adjusted,limit):
    return float(limit)>0 and float(force_adjusted)>float(limit)

def consecutive_over_limit(readings,limit,count=CONSECUTIVE_LIMIT):
    recent=readings[-count:]
    return len(recent)==count and all(
        r.get('status')==READING_ACCEPTED and is_over_limit(r.get('force_adjusted',0.0),limit)
        for r in recent)

def traffic_notice_gate(target,open_notice_count):
    if target=='closed' and int(open_notice_count)<=0:
        raise ConflictError("交通通告缺失或已解除：保留原状态，需关联有效（未解除）交通通告后方可封闭")
    return True
