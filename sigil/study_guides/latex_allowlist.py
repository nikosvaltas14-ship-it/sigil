"""An allowlist for model-written LaTeX: only known-safe commands may appear.

A blocklist of dangerous commands can never be complete (TeX can spell any
command indirectly). This inverts it: every control sequence, environment,
package and library in the document must be on the lists below, or be defined
by the document itself with \\newcommand / \\newtcolorbox / ... (whose bodies
are checked by the same rule, since every token of the document is). Nothing
here reads or writes files, runs programs or builds command names from text;
the lists were built from the commands real guides use.

`allowlist_problem(latex)` returns why the document is refused, or "".
"""
from __future__ import annotations

import re

PACKAGES = frozenset("""
amsmath amssymb amsthm mathtools array bm booktabs circuitikz enumitem esint fontspec
geometry hyperref parskip polyglossia tabularx tcolorbox tikz xcolor multirow cancel
siunitx float caption subcaption makecell longtable
""".split())

TIKZ_LIBRARIES = frozenset("""
arrows arrows.meta calc decorations decorations.pathmorphing decorations.markings
decorations.pathreplacing positioning shapes shapes.geometric shapes.misc patterns angles
quotes intersections fit backgrounds matrix through automata
""".split())

TCB_LIBRARIES = frozenset("skins breakable theorems hooks raster fitting xparse most".split())

ENVIRONMENTS = frozenset("""
document center flushleft flushright itemize enumerate description minipage quote
tabular tabular* tabularx array longtable table figure tcolorbox tikzpicture circuitikz
scope equation equation* align align* aligned gather gather* gathered multline multline*
split cases dcases matrix pmatrix bmatrix vmatrix Vmatrix smallmatrix subequations
theorem lemma proof definition example remark
""".split())

COMMANDS = frozenset("""
documentclass usepackage begin end section subsection subsubsection paragraph title author
date maketitle tableofcontents newpage clearpage pagebreak noindent par item emph textbf
textit texttt textrm textsf textsc textnormal textup underline bfseries itshape ttfamily
sffamily rmfamily normalfont upshape mdseries tiny scriptsize footnotesize small normalsize
large Large LARGE huge Huge centering raggedright raggedleft hfill vfill hspace vspace
medskip smallskip bigskip linebreak newline footnote label ref eqref pageref href url
hypersetup setlength addtolength setcounter stepcounter arabic roman Roman alph Alph
textwidth linewidth textheight parindent parskip baselineskip arraystretch
emergencystretch tabcolsep arraycolsep columnsep fboxsep fboxrule multicolumn multirow
hline cline toprule midrule bottomrule cmidrule addlinespace arraybackslash newcolumntype
newcommand renewcommand providecommand newenvironment renewenvironment newtheorem
DeclareMathOperator newtcolorbox renewtcolorbox newtcbtheorem tcbset tcbuselibrary
tcblower tcbline usetikzlibrary tikzset ctikzset pgfmathsetmacro pgfmathparse
pgfmathresult newlength newfontfamily setmainfont setsansfont setmonofont
setmainlanguage setotherlanguage setotherlanguages setdefaultlanguage selectlanguage
foreignlanguage textgreek textlatin textenglish today ldots dots dotsc dotsb cdots vdots
ddots checkmark protect ensuremath phantom hphantom vphantom quad qquad enspace thinspace
negthinspace raisebox makebox mbox fbox framebox parbox rule color textcolor colorbox
fcolorbox definecolor boxed setlist geometry relax
frac dfrac tfrac cfrac sqrt sum prod coprod int iint iiint oint oiint lim limsup liminf
max min sup inf log ln lg exp sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh
coth arg det deg dim gcd hom ker Pr bmod pmod mod left right big Big bigg Bigg bigl bigr
Bigl Bigr biggl biggr Biggl Biggr middle cdot cdotp times div pm mp ast star circ bullet
le leq ge geq neq ne approx equiv sim simeq cong propto ll gg in notin ni subset subseteq
supset supseteq cup cap setminus emptyset varnothing forall exists nexists neg lnot land
lor implies impliedby iff to gets mapsto rightarrow leftarrow leftrightarrow Rightarrow
Leftarrow Leftrightarrow Longrightarrow Longleftarrow Longleftrightarrow longrightarrow
longleftarrow longleftrightarrow longmapsto uparrow downarrow updownarrow Uparrow
Downarrow nearrow searrow swarrow nwarrow rightleftharpoons infty partial nabla angle
measuredangle perp parallel mid nmid prime hbar ell Re Im wp aleph triangle square
degree ominus oplus otimes odot oslash dagger ddagger lfloor rfloor lceil rceil langle
rangle lvert rvert lVert rVert vert Vert backslash colon smallsetminus
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu
xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathbb mathbf mathrm mathit mathcal mathfrak mathsf mathtt mathscr boldsymbol bm vec hat
widehat bar tilde widetilde dot ddot dddot overline underline overbrace underbrace
overset underset stackrel binom dbinom tbinom operatorname mathop limits nolimits
displaystyle textstyle scriptstyle scriptscriptstyle text intertext tag notag nonumber
substack sideset not cancel bcancel xcancel cancelto SI si num qty unit
draw fill filldraw path node coordinate foreach clip shade shadedraw pattern
i j l o O t v u c d b r H k aa AA ss ae AE oe OE x y
""".split())

# Single non-letter control symbols: \\ \{ \} \% \$ \& \# \_ \, \; \! \: \  \' \` \" \^ \~ \- \/ \| \[ \] \( \) \= \. \@
_CONTROL_SYMBOLS = frozenset(list("\\{}%$&#_,;!: '`\"^~-/|[]()=.@>< ") + ["\n", "\t"])

_CS = re.compile(r"\\([A-Za-z]+|.)", re.S)
_BEGIN_END = re.compile(r"\\(?:begin|end)\s*\{([^{}]*)\}")
_USEPACKAGE = re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\]\s*)?\{([^{}]*)\}")
_TIKZLIB = re.compile(r"\\usetikzlibrary\s*\{([^{}]*)\}")
_TCBLIB = re.compile(r"\\tcbuselibrary\s*\{([^{}]*)\}")
_DOCCLASS = re.compile(r"\\documentclass\s*(?:\[[^\]]*\]\s*)?\{([^{}]*)\}")
_CLASSES = frozenset({"article", "extarticle", "report", "scrartcl", "amsart"})

# Only definers that FAIL on a name that already exists (the build halts) may
# add a name to the allowed set. A redefinition (\renewcommand,
# \DeclareMathOperator, \pgfmathsetmacro, \providecommand, ...) can be undone at
# the end of a {...} group, leaving the real primitive in force under a name the
# check took for the document's own.
_DEFINED_CS = re.compile(r"\\(?:newcommand|newlength)\*?\s*\{?\s*\\([A-Za-z]+)")
# fontspec's family commands: names must look like font families, never one
# of TeX's own font primitives.
_DEFINED_FONT = re.compile(r"\\newfontfamily\s*\{?\s*\\([A-Za-z]+)")
_TEX_FONT_PRIMITIVES = frozenset({"font", "fontname", "fontdimen", "nullfont", "textfont",
                                  "scriptfont", "scriptscriptfont", "fontchardp",
                                  "fontcharht", "fontcharic", "fontcharwd"})
MAX_LOOP_VAR = 3
_FOREACH_VARS = re.compile(r"\\foreach\s*((?:\\[A-Za-z]+\s*/?\s*)+)")
_DEFINED_ENV = re.compile(        # new* only, for the same reason as _DEFINED_CS
    r"\\(?:newenvironment|newtcolorbox|newtcbtheorem|newtheorem)\*?\s*"
    r"(?:\[[^\]]*\]\s*)?\{([^{}]+)\}")
# TeX's ^^ notation makes characters at read time: ^^5c is a backslash, so
# "^^5cinput" would be \input without the source ever containing it.
_HAT_HAT = re.compile(r"\^\^")
# Option keys that make tcolorbox/tikz load an image or file by name.
_FILE_KEYS = re.compile(r"(?i)\b(?:watermark\s+graphics|overzoom\s+image|stretch\s+image|"
                        r"tile\s+image|fill\s+image|natural\s+image|graphics\s+(?:options|"
                        r"pages|directory)|image\s*=|file\s*=|filename\s*=|listing\s+file)")


def _names(regex: re.Pattern, text: str) -> set[str]:
    out: set[str] = set()
    for m in regex.finditer(text):
        for group in m.groups():
            if group:
                out.update(x.strip() for x in group.split(",") if x.strip())
    return out


def allowlist_problem(latex: str) -> str:
    if _HAT_HAT.search(latex):
        return "the ^^ character notation is not allowed"
    keys = _FILE_KEYS.search(latex)
    if keys:
        return f"the option {keys.group(0)!r} loads a file"
    for cls in _names(_DOCCLASS, latex):
        if cls not in _CLASSES:
            return f"document class {cls!r} is not allowed"
    for pkg in _names(_USEPACKAGE, latex):
        if pkg not in PACKAGES:
            return f"package {pkg!r} is not allowed"
    for lib in _names(_TIKZLIB, latex):
        if lib not in TIKZ_LIBRARIES:
            return f"TikZ library {lib!r} is not allowed"
    for lib in _names(_TCBLIB, latex):
        if lib not in TCB_LIBRARIES:
            return f"tcolorbox library {lib!r} is not allowed"
    defined = _names(_DEFINED_CS, latex)
    for name in _names(_DEFINED_FONT, latex):
        if "font" not in name or len(name) < 6 or name in _TEX_FONT_PRIMITIVES:
            return f"\\newfontfamily\\{name} is not allowed (name must be a new font family)"
        defined.add(name)
    for m in _FOREACH_VARS.finditer(latex):
        # A loop variable shadows a name only inside the loop; a long one could
        # be a real primitive that is back in force after it. Short names only.
        for var in re.findall(r"\\([A-Za-z]+)", m.group(1)):
            if len(var) > MAX_LOOP_VAR:
                return f"loop variable \\{var} is too long"
            defined.add(var)
    allowed_cs = COMMANDS | defined
    for m in _CS.finditer(latex):
        name = m.group(1)
        if len(name) == 1 and not name.isalpha():
            if name not in _CONTROL_SYMBOLS:
                return f"\\{name!r} is not allowed"
            continue
        if name not in allowed_cs:
            return f"\\{name} is not on the allowed list"
    allowed_env = ENVIRONMENTS | _names(_DEFINED_ENV, latex)
    for env in _names(_BEGIN_END, latex):
        if env not in allowed_env:
            return f"environment {env!r} is not on the allowed list"
    return ""
