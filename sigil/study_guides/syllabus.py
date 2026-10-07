"""The official order of topics per course, for lectures no schedule lists.

Most ΑΠΘ course pages post no lecture-by-lecture schedule, so a lecture the
schedule does not cover gets its topic from here: lecture N of the semester's
M lectures (both counted from the timetable) maps to the same fraction of the
way through the course's topic list. The topics come from the ΤΗΜΜΥ «Οδηγός
Σπουδών 2026-2027» (§6, Περιεχόμενο Μαθημάτων), split into roughly one
lecture's worth each; where the professor's Moodle files show the real
teaching order (electronics1: diodes, then FET, then BJT) that order is used.

It is a guess that drifts if a professor goes faster or slower, so the topic
is always flagged as inferred and the guide says so at the top. A real
schedule entry on the course page always wins over this list.
"""
from __future__ import annotations

from dataclasses import dataclass

SYLLABUS: dict[str, tuple[str, ...]] = {
    "circuits2": (
        "Μαγνητικά συζευγμένα κυκλώματα και μετασχηματιστές",
        "Ισχύς και ενέργεια σε ημιτονοειδή διέγερση, συντελεστής ισχύος και διόρθωσή του",
        "Μέγιστη μεταφορά πραγματικής ισχύος, μέτρηση ισχύος, προσαρμογή σύνθετης αντίστασης",
        "Κυκλώματα με περιοδική μη ημιτονοειδή διέγερση, σειρές Fourier στα κυκλώματα",
        "Συντελεστές μορφής, κυμάτωσης και παραμόρφωσης, ενεργός, άεργος ισχύς και ισχύς "
        "παραμόρφωσης",
        "Τριφασικά κυκλώματα: πηγές, φορτία, συμμετρικά κυκλώματα, ισοδύναμο μονοφασικό",
        "Ισχύς και συντελεστής ισχύος σε τριφασικά κυκλώματα",
        "Ασύμμετρα τριφασικά κυκλώματα, μέθοδος των συμμετρικών συνιστωσών",
        "Τετράπολα: παράμετροι z, y, υβριδικές και μεταφοράς",
        "Υλοποίηση και διασύνδεση τετραπόλων, ανακλώμενες σύνθετες αντιστάσεις και "
        "σύνθετες αντιστάσεις εικόνων",
        "Εξαρτημένες πηγές και ιδανικός τελεστικός ενισχυτής",
        "Αναστρέφουσα και μη αναστρέφουσα συνδεσμολογία, εφαρμογές τελεστικών ενισχυτών",
    ),
    "electronics1": (
        "Βασική θεωρία ημιαγωγών και ιδιότητες ένωσης pn",
        "Δίοδοι και κυκλώματα διόδων",
        "Μονοπολικό τρανζίστορ (FET): εισαγωγή και περιοχές λειτουργίας",
        "Πόλωση FET και σταθεροποίηση του σημείου λειτουργίας",
        "Ενισχυτικές βαθμίδες με FET",
        "Διπολικό τρανζίστορ (BJT): εισαγωγή και περιοχές λειτουργίας",
        "Πόλωση BJT, ευθεία φόρτου DC",
        "Ενισχυτικές βαθμίδες με BJT, ευθεία φόρτου AC, Η-παράμετροι",
        "Ενισχυτές σε ολοκληρωμένα κυκλώματα",
        "Κατασκευή ολοκληρωμένων κυκλωμάτων",
    ),
    "emfield1": (
        "Φορτία, ένταση ηλεκτρικού πεδίου, βαθμωτό δυναμικό, διηλεκτρική μετατόπιση, "
        "ηλεκτρική ροή",
        "Θεμελιώδεις νόμοι του ηλεκτροστατικού πεδίου, εξισώσεις Poisson και Laplace, "
        "οριακές συνθήκες",
        "Διηλεκτρικά: ηλεκτρικό δίπολο, πόλωση, φορτία πόλωσης, δυνάμεις",
        "Τέλειοι αγωγοί, κοιλότητες, θεώρημα αμοιβαιότητας του Green",
        "Πυκνωτές, χωρητικότητα, μερικές χωρητικότητες",
        "Ενέργεια και δυνάμεις στο ηλεκτροστατικό πεδίο, κίνηση φορτισμένων σωματιδίων",
        "Θεώρημα μοναδικότητας, μέθοδος κατοπτρισμού (ηλεκτρικών ειδώλων)",
        "Μέθοδος χωρισμού μεταβλητών και άλλες αναλυτικές μέθοδοι",
        "Πεδίο ροής μόνιμων ρευμάτων: πυκνότητα ρεύματος, εξίσωση συνέχειας, "
        "οριακές συνθήκες, ΗΕΔ",
        "Αντίσταση, νόμοι Ohm και Kirchhoff, πυκνωτές με απώλειες, νόμος Joule",
        "Θεώρημα ελάχιστων θερμικών απωλειών, πυκνότητα ισχύος, γειωτές",
        "Μαγνητοστατικό πεδίο: μαγνητική επαγωγή και ροή, νόμος Biot-Savart, νόμος Ampere",
        "Βαθμωτό και διανυσματικό μαγνητικό δυναμικό, σωληνοειδές, αυτεπαγωγή",
        "Δυνάμεις και ροπές σε ρευματοφόρους αγωγούς, μαγνητικές οριακές συνθήκες",
    ),
    "appliedmath1": (
        "Γραμμικές διαφορικές εξισώσεις 2ης και ανώτερης τάξης: ορισμοί",
        "Μέθοδοι επίλυσης γραμμικών διαφορικών εξισώσεων ανώτερης τάξης",
        "Εισαγωγή στις μη γραμμικές διαφορικές εξισώσεις",
        "Συστήματα γραμμικών διαφορικών εξισώσεων",
        "Εφαρμογές διαφορικών εξισώσεων",
        "Μετασχηματισμός Laplace",
        "Αντίστροφος Laplace, επίλυση ΠΑΤ και ολοκληροδιαφορικών εξισώσεων",
        "Μιγαδική παραγώγιση και αρμονικές συναρτήσεις",
        "Μιγαδική ολοκλήρωση, θεώρημα και ολοκληρωτικός τύπος Cauchy",
        "Σειρές Laurent και ολοκληρωτικά υπόλοιπα",
        "Σειρές Fourier και ολοκλήρωμα Fourier",
        "Μερικές διαφορικές εξισώσεις, μέθοδος χωρισμού μεταβλητών",
        "Εξίσωση Laplace σε ορθογώνιο, δίσκο και άνω ημιεπίπεδο",
        "Μονοδιάστατη εξίσωση διάχυσης θερμότητας και εξίσωση κύματος",
    ),
    "datastructures": (
        "Δεδομένα και πληροφορία, δομή δεδομένων, αλγόριθμος και πολυπλοκότητα",
        "Εισαγωγή στη Java",
        "Πίνακες",
        "Συνδεδεμένες και σειριακές γραμμικές λίστες",
        "Δένδρα: αποθήκευση, αναζήτηση, εισαγωγή και διαγραφή στοιχείων",
        "Ισοζυγισμένα δένδρα",
        "Β-δένδρα",
        "Εφαρμογές δένδρων και σωροί",
        "Μέθοδοι αναζήτησης",
        "Ταύτιση προτύπου σε κείμενο",
        "Κατακερματισμός",
        "Αλγόριθμοι ταξινόμησης",
    ),
    "logicdesign": (
        "Συστήματα αριθμών, μετατροπές, πράξεις, αρνητικοί αριθμοί",
        "Κώδικες ανίχνευσης και διόρθωσης σφαλμάτων",
        "Άλγεβρα Boole: αξιώματα, θεωρήματα, κανονικές μορφές συναρτήσεων",
        "Ελαχιστοποίηση λογικών συναρτήσεων με πίνακες Karnaugh",
        "Αλγόριθμος Quine-McCluskey",
        "Λογικές πύλες BUF, NOT, AND, OR, NAND, NOR, EXOR και πύλες τριών καταστάσεων",
        "Flip-flop SR, JK, D, T και Master-Slave JK",
        "Χρονοκυκλώματα, ασύγχρονοι και σύγχρονοι απαριθμητές",
        "Καταχωρητές PIPO, SIPO, PISO, SISO, FIFO",
        "Κωδικοποιητές, αποκωδικοποιητές, πολυπλέκτες και αποπολυπλέκτες",
        "Μνήμες RAM και ROM/PROM, σήματα ελέγχου εγγραφής και ανάγνωσης",
        "Αριθμητικά κυκλώματα: αθροιστές, πολλαπλασιαστές, ALU, συγκριτές",
        "Γράφοι αριθμητικών υπολογισμών, κυκλώματα RTL και DSP",
        "Διάδρομος BUS, χρονοπολυπλεξία TDM, εκτίμηση χρόνου υπολογισμών, Crossbar",
        "Διαδρομή δεδομένων (data path) και διαδρομή ελέγχου (control path)",
        "Βασική υπολογιστική μηχανή: register file, πυρήνας, επεξεργαστής, ελεγκτής, PLC",
    ),
}


@dataclass(frozen=True)
class Strand:
    """One lecturer's half of a course taught by two people on different days.

    `topics` are (topic, exact Moodle file names) in teaching order: the
    lecturer's notes sit in one Moodle folder under one module id, so only
    file names can pick the right chapter.
    """
    label: str
    topics: tuple[tuple[str, tuple[str, ...]], ...]


# Course key -> timetable weekday -> strand, for a course two lecturers teach on
# different days. Personal to a course's setup, so it comes from config
# (`study_guides_strands`), never from code:
#   {"<course>": {"MO": {"label": "Complex analysis",
#                        "topics": [["<topic>", ["<exact Moodle file name>", ...]], ...]}}}
STRANDS: dict[str, dict[str, Strand]] = {}


def parse_strands(raw) -> dict[str, dict[str, Strand]]:
    """Strands from config; anything malformed is skipped."""
    out: dict[str, dict[str, Strand]] = {}
    if not isinstance(raw, dict):
        return out
    for course, days in raw.items():
        if not isinstance(days, dict):
            continue
        for weekday, spec in days.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("topics"), list):
                continue
            topics = tuple(
                (str(t[0]), tuple(str(f) for f in t[1]))
                for t in spec["topics"]
                if isinstance(t, (list, tuple)) and len(t) == 2 and isinstance(t[1], (list, tuple)))
            if topics:
                out.setdefault(str(course), {})[str(weekday).upper()] = Strand(
                    str(spec.get("label") or course), topics)
    return out


def strand_for(course_key: str, weekday: str,
               strands: dict[str, dict[str, Strand]] | None = None) -> Strand | None:
    return (STRANDS if strands is None else strands).get(course_key, {}).get(weekday)
def _index(lecture: int, total_lectures: int, count: int) -> int:
    return (min(lecture, total_lectures) - 1) * count // total_lectures


def syllabus_topic(course_key: str, lecture: int, total_lectures: int) -> str:
    """The syllabus topic for lecture `lecture` of `total_lectures`, "" if unknown."""
    topics = SYLLABUS.get(course_key, ())
    if not topics or lecture <= 0 or total_lectures <= 0:
        return ""
    return topics[_index(lecture, total_lectures, len(topics))]


def strand_topic(strand: Strand, lecture: int,
                 total_lectures: int) -> tuple[str, tuple[str, ...]]:
    """(topic, files) for lecture `lecture` of `total_lectures` of one strand."""
    if not strand.topics or lecture <= 0 or total_lectures <= 0:
        return "", ()
    return strand.topics[_index(lecture, total_lectures, len(strand.topics))]
