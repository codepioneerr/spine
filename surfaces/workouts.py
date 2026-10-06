"""
surfaces.workouts — quiet, mat-only routines for a dorm room.

General plans, not prescriptions: no jumping, no equipment, no furniture.
Each move states dose, technique and an easier option. Progression is one
small step at a time and only after a session felt "easy" (feedback buttons).
"""

# id -> (name, dose, technique, easier option)
MOVES = {
    "glute_bridge": ("Glute bridge", "3 x 10, 45 s rest",
                     "On your back, knees bent, feet hip-width. Press through heels, lift hips until "
                     "body is a straight line knees-to-shoulders, pause 1 s, lower slowly.",
                     "Smaller range; or hold the top for 10 s instead of reps."),
    "split_squat": ("Split squat (hold a wall if needed)", "3 x 8 per side, 60 s rest",
                    "Long stride stance on the mat. Lower the back knee straight down, front knee "
                    "tracks over the 2nd-3rd toe, torso tall. Stand back up slowly. No noise.",
                    "Shorter range; hand on the wall for balance."),
    "incline_pushup": ("Push-up (knees or hands on bed frame edge if stable)", "3 x 8, 60 s rest",
                       "Hands under shoulders, body in one line, lower chest with control, push back up.",
                       "Knees down, or hands against a wall."),
    "dead_bug": ("Dead bug", "3 x 8 per side, 45 s rest",
                 "On back, arms up, knees over hips. Slowly extend opposite arm and leg while "
                 "keeping the low back gently on the mat. Breathe out as you extend.",
                 "Move only the legs, or only tap heels down."),
    "side_plank": ("Side plank", "2 x 20 s per side",
                   "On forearm, elbow under shoulder, hips lifted so body is straight.",
                   "Knees bent and down."),
    "calf_raise": ("Slow calf raise", "3 x 12, 30 s rest",
                   "Stand near a wall, rise onto the balls of the feet for 2 s, lower for 3 s.",
                   "Both hands on the wall; smaller range."),
    "single_leg_balance": ("Single-leg balance", "3 x 30 s per side",
                           "Stand on one foot, knee soft, keep the foot's arch from collapsing.",
                           "Fingertips on a wall."),
    "ankle_circles": ("Ankle circles + knee-to-wall", "1 min per side",
                      "Half-kneeling facing a wall, slide the front knee toward the wall over the "
                      "toes with the heel down; then slow ankle circles.",
                      "Seated ankle circles only."),
    "hip_9090": ("90/90 hip switches", "2 x 6 per side",
                 "Sit with both knees bent 90 degrees to one side; rotate knees to the other "
                 "side, hands behind for support.",
                 "Keep hands down and move slowly through a smaller range."),
    "hip_flexor": ("Half-kneeling hip flexor stretch", "2 x 30 s per side",
                   "Kneel on mat (fold it for padding), squeeze the back glute, shift hips "
                   "forward slightly until you feel the front of the hip.",
                   "Smaller lunge; cushion under knee."),
    "cat_cow": ("Cat-cow", "1 min", "On hands and knees, slowly round then gently arch the back.",
                "Smaller range."),
    "hamstring_floss": ("Lying hamstring stretch", "2 x 30 s per side",
                        "On back, one leg up, hands behind thigh, straighten the knee until a "
                        "mild stretch.", "Keep the knee bent more."),
}

PLANS = {
    "strength15": ("Quiet 15-minute strength (mat)", 15,
                   ["glute_bridge", "split_squat", "incline_pushup", "dead_bug", "side_plank"],
                   "2 min warm-up: march in place softly + cat-cow. Do the moves in order; "
                   "~15 min total."),
    "mobility10": ("10-minute ankle, knee and hip mobility", 10,
                   ["ankle_circles", "calf_raise", "single_leg_balance", "hip_9090", "hip_flexor",
                    "hamstring_floss"],
                   "Move slowly, stay in a comfortable range, breathe normally."),
    "walkprep8": ("8-minute walking prep", 8,
                  ["ankle_circles", "calf_raise", "glute_bridge", "single_leg_balance"],
                  "Good before a walk. Mild effort only."),
}

SAFETY = ("Stop and get checked if something causes sharp pain, swelling, numbness or tingling, "
          "or pain that lingers into the next day. Mild muscle effort is fine.")


def render(plan_id: str, profile_notes: list[str] | None = None) -> str:
    name, minutes, moves, intro = PLANS[plan_id]
    lines = [f"<b>{name}</b> (~{minutes} min, mat only, no jumping)", intro, ""]
    for i, m in enumerate(moves, 1):
        n, dose, how, easy = MOVES[m]
        lines.append(f"{i}. <b>{n}</b> — {dose}\n   {how}\n   Easier: {easy}")
    if profile_notes:
        lines.append("")
        lines += profile_notes
    lines.append("")
    lines.append(SAFETY)
    return "\n".join(lines)


def profile_notes(profile_text: str) -> list[str]:
    """Personalisation from the self-reported profile only (var/health_profile.md).
    Adds cues; never infers anything new."""
    t = (profile_text or "").lower()
    out = []
    if "valgus" in t or "knock-knee" in t:
        out.append("You noted knees that tend to come inward: in split squats and bridges, keep "
                   "the knee pointing over the 2nd-3rd toe; slower reps help.")
    if "flat feet" in t:
        out.append("You noted flat feet: in balance and calf raises, keep weight spread across the "
                   "big toe, little toe and heel.")
    if "muay thai" in t or "boxing" in t:
        out.append("On hard sparring/training days, use the mobility plan instead of strength.")
    if out:
        out.insert(0, "<i>Personalised from your saved profile (self-reported):</i>")
    return out
