SYNTHETIC Moodle web-service responses for the study-guides pipeline.

These files were written by hand to match the documented response shapes of
Moodle 4.x (core_webservice_get_site_info, core_enrol_get_users_courses,
core_course_get_contents, login/token.php). None of them came from
elearning.auth.gr: every name, id, URL, date and token here is invented. The
token in token_ok.json is fake.

Replace them with recorded responses once `python -m sigil.study_guides
discover` has run against the real site (spec section 11: "record Moodle
responses once and use them as fixtures"). Before committing a recording,
strip anything personal and every token: the `userprivateaccesskey` field of
site_info, and any `token=` left in a fileurl.

Files
  site_info.json                core_webservice_get_site_info (userid 48213)
  user_courses.json             core_enrol_get_users_courses: the five courses
                                (names in mixed case/accents, as ΑΠΘ writes
                                them) plus one unrelated info course
  course_contents_18431.json    Ηλεκτρικά Κυκλώματα ΙΙ: schedule in the
                                section summaries ("Εβδομάδα 3",
                                "Τρίτη 13/10: Θεωρήματα Thevenin και Norton"),
                                a past-exams PDF, a URL module, a folder with
                                a Spice lab and a hidden (uservisible=false)
                                future lecture
  course_contents_18455.json    Δομές Δεδομένων: schedule as an HTML table in a
                                label module, a schedule-looking PDF, a
                                past-exams PDF, a .pptx, a .zip and a page
                                module (description + index.html)
  error_invalidtoken.json       the body Moodle returns (HTTP 200) for a bad
                                or expired token
  token_ok.json                 login/token.php success
  token_invalidlogin.json       login/token.php rejection
  action_events.json            core_calendar_get_action_events_by_timesort:
                                the whole timeline in one reply, for a mock
                                server that filters it by timesortfrom/to and
                                pages it by aftereventid/limitnum. Relative to
                                "now" = 2026-10-14 12:00 +03:00: an April 2026
                                assignment (course 17002, last year's; overdue,
                                not actionable) far outside a 7-day lookback,
                                an overdue-but-actionable one from 10-13, two
                                quizzes and two more assignments inside 21
                                days, and a quiz on 12-10 past the horizon.
                                `instance` equals the ?id= of `url` (the cmid),
                                as on the real site
  assign_assignments.json       mod_assign_get_assignments for 18431/18437/
                                18455: assign ids and cut-offs keyed by cmid,
                                plus two warningcode "1" ("No access rights in
                                module context") warnings to be ignored
  assign_submission_status.json mod_assign_get_submission_status for assign
                                30155: status "submitted"

Dates are for the 2026-27 winter semester (Tuesday 2026-10-13 and so on);
the weekdays in the Greek text match the real calendar.
