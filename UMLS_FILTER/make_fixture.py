#!/usr/bin/env python3
"""Tiny synthetic RRF fixture so the extractor can be smoke-tested offline."""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture")
os.makedirs(OUT, exist_ok=True)

# CUI, SAB, TTY, CODE, STR, TS, STT, ISPREF, AUI, SUPPRESS
CONSO = [
    # --- myocardial infarction -------------------------------------------
    ("C0027051", "MTH", "PN", "NOCODE", "Myocardial Infarction", "P", "PF", "Y", "A0001", "N"),
    ("C0027051", "SNOMEDCT_US", "FN", "22298006", "Myocardial infarction (disorder)", "S", "PF", "N", "A0002", "N"),
    ("C0027051", "SNOMEDCT_US", "PT", "22298006", "Myocardial infarction", "P", "PF", "Y", "A0003", "N"),
    ("C0027051", "SNOMEDCT_US", "SY", "22298006", "Cardiac infarction", "S", "PF", "N", "A0004", "N"),
    ("C0027051", "SNOMEDCT_US", "SY", "22298006", "Infarction of heart", "S", "PF", "N", "A0005", "N"),
    ("C0027051", "CHV", "SY", "0000123", "heart attack", "S", "PF", "N", "A0006", "N"),
    ("C0027051", "MSH", "ET", "D009203", "Coronary Thrombosis", "S", "PF", "N", "A0007", "N"),
    ("C0027051", "SNOMEDCT_US", "AB", "22298006", "MI", "S", "PF", "N", "A0008", "N"),
    ("C0027051", "NCI", "AB", "C27996", "AMI", "S", "PF", "N", "A0009", "N"),
    ("C0027051", "ICD10CM", "PT", "I21.9", "Acute myocardial infarction, unspecified", "S", "PF", "N", "A0010", "N"),
    ("C0027051", "ICD10CM", "HT", "I21", "Acute myocardial infarction", "S", "PF", "N", "A0011", "N"),
    ("C0027051", "MSH", "PM", "D009203", "Heart Attack [Disease/Finding]", "S", "PF", "N", "A0012", "Y"),  # suppressed
    ("C0027051", "MSH", "SY", "D009203", "Myocardial infarct that is really an absurdly long descriptor string exceeding the cap", "S", "PF", "N", "A0013", "N"),
    # --- hypertension ----------------------------------------------------
    ("C0020538", "MTH", "PN", "NOCODE", "Hypertensive disease", "P", "PF", "Y", "A0101", "N"),
    ("C0020538", "SNOMEDCT_US", "PT", "38341003", "Hypertensive disorder, systemic arterial", "P", "PF", "Y", "A0102", "N"),
    ("C0020538", "SNOMEDCT_US", "SY", "38341003", "High blood pressure", "S", "PF", "N", "A0103", "N"),
    ("C0020538", "SNOMEDCT_US", "SY", "38341003", "Arterial hypertension", "S", "PF", "N", "A0104", "N"),
    ("C0020538", "CHV", "SY", "0000456", "high blood pressure disorder", "S", "PF", "N", "A0105", "N"),
    ("C0020538", "SNOMEDCT_US", "AB", "38341003", "HTN", "S", "PF", "N", "A0106", "N"),
    ("C0020538", "ICD10CM", "HT", "I10", "Essential (primary) hypertension", "S", "PF", "N", "A0107", "N"),
    # --- chest pain ------------------------------------------------------
    ("C0008031", "MTH", "PN", "NOCODE", "Chest Pain", "P", "PF", "Y", "A0201", "N"),
    ("C0008031", "SNOMEDCT_US", "PT", "29857009", "Chest pain", "P", "PF", "Y", "A0202", "N"),
    ("C0008031", "SNOMEDCT_US", "SY", "29857009", "Thoracic pain", "S", "PF", "N", "A0203", "N"),
    ("C0008031", "SNOMEDCT_US", "SY", "29857009", "Retrosternal pain", "S", "PF", "N", "A0204", "N"),
    ("C0008031", "CHV", "SY", "0000789", "chest discomfort", "S", "PF", "N", "A0205", "N"),
    ("C0008031", "ICD10CM", "HT", "R07", "Pain in throat and chest", "S", "PF", "N", "A0206", "N"),
    # --- ambiguity trap: MS spread across four CUIs ----------------------
    ("C0026769", "MTH", "PN", "NOCODE", "Multiple Sclerosis", "P", "PF", "Y", "A0301", "N"),
    ("C0026769", "SNOMEDCT_US", "SY", "24700007", "Disseminated sclerosis", "S", "PF", "N", "A0302", "N"),
    ("C0026769", "SNOMEDCT_US", "AB", "24700007", "MS", "S", "PF", "N", "A0303", "N"),
    ("C0026266", "MTH", "PN", "NOCODE", "Mitral Valve Stenosis", "P", "PF", "Y", "A0401", "N"),
    ("C0026266", "SNOMEDCT_US", "AB", "79619009", "MS", "S", "PF", "N", "A0402", "N"),
    ("C0026266", "SNOMEDCT_US", "SY", "79619009", "Mitral stenosis", "S", "PF", "N", "A0403", "N"),
    ("C0026549", "MTH", "PN", "NOCODE", "Morphine Sulfate", "P", "PF", "Y", "A0501", "N"),
    ("C0026549", "RXNORM", "AB", "7052", "MS", "S", "PF", "N", "A0502", "N"),
    ("C0026549", "RXNORM", "SY", "7052", "morphine sulphate", "S", "PF", "N", "A0503", "N"),
    ("C0026549", "RXNORM", "IN", "7052", "morphine sulfate", "P", "PF", "Y", "A0504", "N"),
    ("C0026549", "RXNORM", "BN", "203032", "MS Contin", "S", "PF", "N", "A0505", "N"),
    # dose-level product strings: must NOT become synonyms of the ingredient
    ("C0026549", "RXNORM", "SCD", "894801", "morphine sulfate 15 MG oral tablet", "S", "PF", "N", "A0506", "N"),
    ("C0026549", "RXNORM", "SBD", "894802", "morphine sulfate 15 MG oral tablet [MS Contin]", "S", "PF", "N", "A0507", "N"),
    ("C0026549", "RXNORM", "PSN", "894801", "morphine sulfate 15 MG 12 HR extended release oral tablet", "S", "PF", "N", "A0508", "N"),
    # --- a second drug, to prove the partition isn't a fluke -------------
    ("C0002645", "MTH", "PN", "NOCODE", "Amoxicillin", "P", "PF", "Y", "A0801", "N"),
    ("C0002645", "RXNORM", "IN", "723", "amoxicillin", "P", "PF", "Y", "A0802", "N"),
    ("C0002645", "RXNORM", "BN", "151392", "Amoxil", "S", "PF", "N", "A0803", "N"),
    ("C0002645", "RXNORM", "SY", "723", "amoxycillin", "S", "PF", "N", "A0804", "N"),
    ("C0002645", "MSH", "ET", "D000658", "p-Hydroxyampicillin", "S", "PF", "N", "A0805", "N"),
    ("C0002645", "RXNORM", "SCD", "308182", "amoxicillin 500 MG oral capsule", "S", "PF", "N", "A0806", "N"),
    # --- a procedure -----------------------------------------------------
    ("C0007203", "MTH", "PN", "NOCODE", "Cardiopulmonary Resuscitation", "P", "PF", "Y", "A0901", "N"),
    ("C0007203", "SNOMEDCT_US", "PT", "89666000", "Cardiopulmonary resuscitation", "P", "PF", "Y", "A0902", "N"),
    ("C0007203", "SNOMEDCT_US", "SY", "89666000", "Cardiac resuscitation", "S", "PF", "N", "A0903", "N"),
    ("C0007203", "SNOMEDCT_US", "AB", "89666000", "CPR", "S", "PF", "N", "A0904", "N"),
    ("C0007203", "CHV", "SY", "0000901", "restart the heart", "S", "PF", "N", "A0905", "N"),
    ("C0025355", "MTH", "PN", "NOCODE", "Mental Status", "P", "PF", "Y", "A0601", "N"),
    ("C0025355", "SNOMEDCT_US", "AB", "358974004", "MS", "S", "PF", "N", "A0602", "N"),
    ("C0025355", "SNOMEDCT_US", "SY", "358974004", "Mental state", "S", "PF", "N", "A0603", "N"),
    # --- non-English row that must be filtered out -----------------------
    ("C0027051", "MSHSPA", "PT", "D009203", "Infarto del Miocardio", "S", "PF", "N", "A0701", "N"),
]

STY = [
    ("C0027051", "T047", "B2.2.1.2.1", "Disease or Syndrome"),
    ("C0020538", "T047", "B2.2.1.2.1", "Disease or Syndrome"),
    ("C0008031", "T184", "B2.2.1.2", "Sign or Symptom"),
    ("C0026769", "T047", "B2.2.1.2.1", "Disease or Syndrome"),
    ("C0026266", "T047", "B2.2.1.2.1", "Disease or Syndrome"),
    ("C0026549", "T121", "A1.3.3", "Pharmacologic Substance"),
    ("C0025355", "T033", "B2.2.1.2", "Finding"),
    ("C0002645", "T121", "A1.3.3", "Pharmacologic Substance"),
    ("C0002645", "T195", "A1.3.3", "Antibiotic"),
    ("C0007203", "T061", "B1.3.1.3", "Therapeutic or Preventive Procedure"),
]

# CUI, AUI, PAUI, SAB, PTR  (dot-joined AUI path to root)
HIER = [
    ("C0027051", "A0003", "A9000", "SNOMEDCT_US", "A9001.A9002.A9003.A9004.A9005.A9006.A9007.A9000"),
    ("C0020538", "A0102", "A9100", "SNOMEDCT_US", "A9001.A9002.A9003.A9100"),
    ("C0008031", "A0202", "A9200", "SNOMEDCT_US", "A9001.A9002.A9200"),
    ("C0026769", "A0301", "A9300", "SNOMEDCT_US", "A9001.A9002.A9003.A9004.A9300"),
    ("C0026266", "A0401", "A9400", "SNOMEDCT_US", "A9001.A9002.A9003.A9004.A9401.A9400"),
    ("C0025355", "A0601", "A9500", "SNOMEDCT_US", "A9001.A9500"),
    ("C0007203", "A0902", "A9600", "SNOMEDCT_US", "A9001.A9002.A9600"),
]


def w(name, rows):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write("|".join(r) + "|\n")
    print("wrote", name, len(rows), "rows")


w("MRCONSO.RRF", [
    (cui, "SPA" if sab == "MSHSPA" else "ENG", ts, "L0", stt, "S0", ispref,
     aui, "", code, "", sab, tty, code, s, "0", sup, "")
    for (cui, sab, tty, code, s, ts, stt, ispref, aui, sup) in CONSO
])
w("MRSTY.RRF", [(c, t, n, s, "AT0", "") for (c, t, n, s) in STY])
w("MRHIER.RRF", [(c, a, "1", p, sab, "isa", ptr, "", "")
                 for (c, a, p, sab, ptr) in HIER])
