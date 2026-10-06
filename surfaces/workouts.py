"""
surfaces.workouts — quiet, mat-only routines for a dorm room.

General plans, not prescriptions: no jumping, no equipment, no furniture.
Durations are COMPUTED, never stated: work time (reps x tempo, or holds,
both sides when per-side) + rests between sets + side switches + a
transition between moves + the warm-up. The plan title shows the computed
total, so "15 minutes" cannot quietly mean 21.
"""

from __future__ import annotations

from dataclasses import dataclass

TEMPO_S = 4          # seconds per controlled rep (about 2 s down, 1 s pause, 1 s up)
SWITCH_S = 10        # changing sides within a set
TRANSITION_S = 30    # getting into position for the next move


@dataclass(frozen=True)
class Move:
    name: str
    sets: int
    reps: int | None          # reps per set (per side if per_side)
    hold_s: int | None        # or a hold per set (per side if per_side)
    per_side: bool
    rest_s: int               # rest between sets
    how: str
    easier: str

    def work_s(self) -> int:
        one = (self.reps or 0) * TEMPO_S + (self.hold_s or 0)
        return one * (2 if self.per_side else 1) + (SWITCH_S if self.per_side else 0)

    def seconds(self) -> int:
        return self.sets * self.work_s() + (self.sets - 1) * self.rest_s

    def dose(self) -> str:
        unit = f"{self.reps} reps" if self.reps else f"{self.hold_s} s hold"
        side = " per side" if self.per_side else ""
        rest = f", rest {self.rest_s} s between sets" if self.sets > 1 and self.rest_s else ""
        return f"{self.sets} × {unit}{side}{rest}"


MOVES = {
    "glute_bridge": Move("Glute bridge", 2, 12, None, False, 45,
                         "On your back, knees bent, feet hip-width. Press through heels, lift hips until "
                         "body is a straight line knees-to-shoulders, pause 1 s, lower slowly.",
                         "Smaller range; or hold the top for 10 s instead of reps."),
    "split_squat": Move("Split squat (hold a wall if needed)", 2, 8, None, True, 45,
                        "Long stride stance on the mat. Lower the back knee straight down, front knee "
                        "tracks over the 2nd–3rd toe, torso tall. Stand back up slowly. No noise.",
                        "Shorter range; hand on the wall for balance."),
    "incline_pushup": Move("Push-up (knees, or hands on a wall)", 2, 8, None, False, 45,
                           "Hands under shoulders, body in one line, lower chest with control, push back up.",
                           "Hands against a wall."),
    "dead_bug": Move("Dead bug", 2, 6, None, True, 30,
                     "On back, arms up, knees over hips. Slowly extend opposite arm and leg while "
                     "keeping the low back gently on the mat. Breathe out as you extend.",
                     "Move only the legs, or only tap heels down."),
    "side_plank": Move("Side plank", 2, None, 20, True, 30,
                       "On forearm, elbow under shoulder, hips lifted so body is straight.",
                       "Knees bent and down."),
    "calf_raise": Move("Slow calf raise", 2, 12, None, False, 30,
                       "Stand near a wall, rise onto the balls of the feet for 2 s, lower for 3 s.",
                       "Both hands on the wall; smaller range."),
    "single_leg_balance": Move("Single-leg balance", 2, None, 30, True, 15,
                               "Stand on one foot, knee soft, keep the foot's arch from collapsing.",
                               "Fingertips on a wall."),
    "ankle_circles": Move("Ankle circles + knee-to-wall", 1, None, 60, True, 0,
                          "Half-kneeling facing a wall, slide the front knee toward the wall over the "
                          "toes with the heel down; then slow ankle circles.",
                          "Seated ankle circles only."),
    "hip_9090": Move("90/90 hip switches", 2, 6, None, False, 20,
                     "Sit with both knees bent 90 degrees to one side; rotate knees to the other "
                     "side, hands behind for support.",
                     "Keep hands down and move slowly through a smaller range."),
    "hip_flexor": Move("Half-kneeling hip flexor stretch", 2, None, 30, True, 0,
                       "Kneel on mat (fold it for padding), squeeze the back glute, shift hips "
                       "forward slightly until you feel the front of the hip.",
                       "Smaller lunge; cushion under knee."),
    "hamstring_floss": Move("Lying hamstring stretch", 1, None, 30, True, 0,
                            "On back, one leg up, hands behind thigh, straighten the knee until a "
                            "mild stretch.", "Keep the knee bent more."),
}

# plan id -> (name, warm-up seconds, warm-up text, moves)
PLANS = {
    "strength15": ("Quiet strength (mat)", 120,
                   "Warm-up 2 min: march in place softly, then cat-cow on hands and knees.",
                   ["glute_bridge", "split_squat", "incline_pushup", "dead_bug", "side_plank"]),
    "mobility10": ("Ankle, knee and hip mobility", 60,
                   "Warm-up 1 min: easy marching. Move slowly, stay in a comfortable range.",
                   ["ankle_circles", "calf_raise", "single_leg_balance", "hip_9090", "hip_flexor",
                    "hamstring_floss"]),
    "walkprep8": ("Walking prep", 0, "Mild effort only; good right before a walk.",
                  ["ankle_circles", "calf_raise", "glute_bridge", "single_leg_balance"]),
}

SAFETY = ("Stop and get checked if something causes sharp pain, swelling, numbness or tingling, "
          "or pain that lingers into the next day. Mild muscle effort is fine.")


def plan_seconds(plan_id: str) -> int:
    _, warm, _, moves = PLANS[plan_id]
    return warm + sum(MOVES[m].seconds() for m in moves) + TRANSITION_S * (len(moves) - 1)


def _mmss(s):
    m, s = divmod(int(round(s)), 60)
    return f"{m}:{s:02d}"


def render(plan_id: str, profile_notes: list[str] | None = None) -> str:
    name, warm, intro, moves = PLANS[plan_id]
    total = plan_seconds(plan_id)
    lines = [f"<b>{name}</b> — about {round(total / 60)} min total (mat only, no jumping)",
             f"<i>Includes rests and {TRANSITION_S} s transitions; reps at ~{TEMPO_S} s each.</i>",
             intro, ""]
    for i, m in enumerate(moves, 1):
        mv = MOVES[m]
        lines.append(f"{i}. <b>{mv.name}</b> — {mv.dose()} (≈{_mmss(mv.seconds())})\n"
                     f"   {mv.how}\n   Easier: {mv.easier}")
    if profile_notes:
        lines.append("")
        lines += profile_notes
    lines.append("")
    lines.append(SAFETY)
    return "\n".join(lines)


def shorter(plan_id: str) -> str:
    """A 1-set version of the same plan for busy days."""
    name, warm, intro, moves = PLANS[plan_id]
    secs = warm + sum(MOVES[m].work_s() for m in moves) + TRANSITION_S * (len(moves) - 1)
    lines = [f"<b>{name} — short version</b>, about {round(secs / 60)} min: one set of each move, "
             "no rests between sets.", intro]
    for m in moves:
        mv = MOVES[m]
        unit = f"{mv.reps} reps" if mv.reps else f"{mv.hold_s} s"
        lines.append(f"• {mv.name}: 1 × {unit}{' per side' if mv.per_side else ''}")
    return "\n".join(lines)


def profile_notes(confirmed: dict, plan_id: str) -> list[str]:
    """Cues from CONFIRMED profile items only (surfaces.profile). Plan-aware:
    a cue appears only if the plan contains a move it applies to."""
    moves = set(PLANS[plan_id][3])
    out = []
    if confirmed.get("knee_valgus") and moves & {"split_squat", "glute_bridge", "single_leg_balance"}:
        out.append("Knees that tend to move inward (you confirmed): keep the knee pointing over "
                   "the 2nd–3rd toe in split squats, bridges and balance work; slow reps help.")
    if confirmed.get("flat_feet") and moves & {"single_leg_balance", "calf_raise"}:
        out.append("Flat feet (you confirmed): in balance and calf raises, keep weight spread across "
                   "the big toe, little toe and heel.")
    if confirmed.get("combat_training") and plan_id == "strength15":
        out.append("On hard boxing/Muay Thai days, choose /mobility instead of this.")
    if out:
        out.insert(0, "<i>Personalised from your confirmed profile:</i>")
    return out
