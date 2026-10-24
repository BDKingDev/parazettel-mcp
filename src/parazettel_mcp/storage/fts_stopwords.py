"""English stopwords dropped from full-text queries and indexed text.

This is the stopword list Kuzu 0.11.3's FTS extension applied by default (the
SMART English list, also used by DuckDB), extracted so full-text behaviour stays
the same after the move to SQLite FTS5, which has no stopword support of its
own. Contractions are stored with the apostrophe removed ("aint"), matching how
Kuzu's ignore pattern stripped punctuation before matching.
"""

ENGLISH_STOPWORDS = frozenset(
    """
    a able about above according accordingly across actually after
    afterwards again against aint all allow allows almost alone along
    already also although always am among amongst an and another any anybody
    anyhow anyone anything anyway anyways anywhere apart appear appreciate
    appropriate are arent around as aside ask asking associated at available
    away awfully be became because become becomes becoming been before
    beforehand behind being believe below beside besides best better between
    beyond both brief but by came can cannot cant cause causes certain
    certainly changes clearly cmon co com come comes concerning consequently
    consider considering contain containing contains corresponding could
    couldnt course cs currently definitely described despite did didnt
    different do does doesnt doing done dont down downwards during e each
    edu eg eight either else elsewhere enough entirely especially et etc
    even ever every everybody everyone everything everywhere ex exactly
    example except far few fifth first five followed following follows for
    former formerly forth four from further furthermore get gets getting
    given gives go goes going gone got gotten greetings had hadnt happens
    hardly has hasnt have havent having he hello help hence her here
    hereafter hereby herein heres hereupon hers herself hes hi him himself
    his hither hopefully how howbeit however id ie if ignored ill im
    immediate in inasmuch inc indeed indicate indicated indicates inner
    insofar instead into inward is isnt it itd itll its itself ive j just k
    keep keeps kept know known knows last lately later latter latterly least
    less lest let lets like liked likely little look looking looks ltd
    mainly many may maybe me mean meanwhile merely might more moreover most
    mostly much must my myself n namely nd near nearly necessary need needs
    neither never nevertheless new next nine no nobody non none noone nor
    normally not nothing novel now nowhere o obviously of off often oh ok
    okay old on once one ones only onto or other others otherwise ought our
    ours ourselves out outside over overall own p particular particularly
    per perhaps placed please plus possible presumably probably provides q
    que quite qv r rather rd re really reasonably regarding regardless
    regards relatively respectively right said same saw say saying says
    secondly see seeing seem seemed seeming seems seen self selves sensible
    sent serious seriously seven several shall she should shouldnt since six
    so some somebody somehow someone something sometime sometimes somewhat
    somewhere soon sorry specified specify specifying still sub such sup
    sure t take taken tell tends th than thank thanks thanx that thats the
    their theirs them themselves then thence there thereafter thereby
    therefore therein theres thereupon these they theyd theyll theyre theyve
    think third this thorough thoroughly those though three through
    throughout thru thus to together too took toward towards tried tries
    truly try trying ts twice two un under unfortunately unless unlikely
    until unto up upon use used useful uses using usually uucp v various
    very via viz vs want wants was wasnt way we wed welcome well went were
    werent weve what whatever whats when whence whenever where whereafter
    whereas whereby wherein wheres whereupon wherever whether which while
    whither who whoever whole whom whos whose why will willing wish with
    within without wonder wont would wouldnt x yes yet you youd youll your
    youre yours yourself yourselves youve zero
    """.split()
)
