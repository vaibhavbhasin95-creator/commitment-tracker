"""
Commitment labels for training_data/utterances_for_labelling.csv (first pass by
Claude, 2026-10-01 — for the team to review). Every item_id not listed under
ACTION is DIALOG.

Definition used
---------------
ACTION  — a specific person (the speaker, a named person, or an explicitly
          addressed group) commits to, is assigned, or accepts a concrete task
          to be done AFTER the meeting. Written action-item entries that name
          an owner count.
DIALOG  — everything else: discussion, questions, opinions, proposals nobody
          accepted, decisions with no owner, meeting navigation ("let's move
          to..."), things done during the meeting itself ("I'll recap",
          "I'll share my screen", an in-class exercise), past actions, generic
          advice, and contingencies ("if X slips, I'll escalate", "drop me an
          email and I'll help").
"""

ACTION = {
    # fy2027 (13 of 109)
    2963: "Sarah: I need to run a full skills audit with HR",
    2985: "Bhuvan assigns everyone: research, consult, deliver models by month end",
    2986: "Abhinav (Due Sept 26): market research on comp bands",
    2987: "Abhinav: work with HR on merit increment matrix",
    2988: "Kevin (Due Sept 27): model variable-bonus feasibility",
    2989: "Sarah (Due Sept 28): HR-wide workforce audit",
    2990: "Sarah: evaluate skill-gap risk, deliver revised hiring plan",
    2991: "Francoise (Due Sept 29): finalize culture budget proposal",
    2992: "Aman (Due Sept 29): tiered AI investment forecast",
    2993: "All five submit raw reports to Sarah by due dates",
    2994: "Sarah: collate inputs, consolidated status report",
    2995: "Sarah: I'll track deliverables and give master report on the 30th",
    2996: "Bhuvan: I will issue a calendar invite for the next review",
    # template_numbered (22 of 302)
    42: "I'll coordinate the reconciliation", 52: "same", 68: "same", 147: "same", 157: "same",
    43: "I'll provide the operations view", 53: "same", 69: "same", 84: "same", 98: "same", 199: "same",
    123: "action item list", 124: "action item list", 125: "action item list", 126: "action item list",
    127: "action item list", 158: "action item list", 180: "action item list", 217: "action item list",
    218: "action item list", 236: "action item list",
    134: "I'll circulate the notes and action tracker after this call",
    # template_corpus (81 of 390)
    372: "I will own the test plan and nominate reviewers by tomorrow",
    375: "I'll include thresholds for failure rate...",
    382: "I will complete the next step ... circulate an update by <date>", 387: "same", 389: "same",
    391: "same", 393: "same", 395: "same",
    418: "I'll consolidate the notes",
    **{i: "action item list" for i in [
        *range(403, 409), *range(439, 445), *range(464, 470), *range(489, 495), *range(514, 520),
        *range(539, 545), *range(564, 570), *range(589, 595), *range(614, 620), *range(638, 644),
        *range(663, 669), *range(687, 693)]},
    # workshop_clustering (12 of 234)
    871: "I am adding a segmentation workstream to the product plan",
    878: "I will add a stability check", 879: "I will compare text representation with embeddings",
    883: "I will review the submissions and return feedback",
    909: "action list", 911: "action list", 912: "action list", 913: "action list",
    914: "action list", 915: "action list", 916: "action list", 917: "action list",
    # workshop_validation (11 of 152)
    1052: "each team should finalize a working feature list",
    1055: "We will document the baseline metrics first",
    1056: "I will add class distribution ... to the notebook",
    1057: "I will run multiple train-test splits", 1058: "I will identify high-impact mistakes",
    1068: "action list", 1069: "action list", 1070: "action list", 1071: "action list",
    1073: "action list", 1074: "action list",
    # real_lecture (19 of 1809)
    1088: "written: product and technology owners to confirm data access...",
    2099: "students: create a Google Doc and Sheet and share both",
    2118: "we'll give you feedback", 2120: "I'll ask the team",
    2121: "once 5-10 are submitted I will take a look and get back",
    2128: "we will give suggestions on the team proposal", 2129: "we'll review and give feedback",
    2146: "students: decide the list of features",
    2216: "students: do the DIY task later",
    2314: "I will share some material with you",
    2323: "students: do the train/test splitting on digital payments",
    2568: "another DIY task for you (homework)",
    2717: "students: do the digital payments part later",
    2787: "students: take a look at it later",
    2798: "I will share with the lead-up team",
    2831: "students: go through this later",
    2868: "I will share the link on the LMS",
    2878: "I will check with the lead-up team",
    2880: "students: you need to submit (the form)",
}

# Close calls labelled DIALOG, listed so reviewers can overturn them.
BORDERLINE_DIALOG = {
    9: "I'll call out decisions as we make them — happens during the meeting",
    308: "I will pause whenever we reach a decision — during the meeting",
    378: "I'll recap what I heard — during the meeting",
    385: "if input isn't available, I will escalate — contingency",
    884: "if something needs correcting, I will flag it — contingency",
    2879: "if they haven't shared a form, I will ask them — contingency",
    2109: "let us know and we will help — contingency",
    2113: "drop me a message, I will respond — contingency",
    2709: "drop me an email, I will help — contingency",
    2843: "drop me a message, I will try to help — contingency",
    2327: "student: I'll go through the class again — personal study intention",
    1884: "I'll share the notebook — shared during the session",
    2296: "I will share this notebook with you — shared during the session",
    2081: "your DIY task, which you need to do right now — in-session exercise",
    2523: "DIY: try to predict ... — in-session exercise",
    2838: "please do practice with it — general advice",
}
