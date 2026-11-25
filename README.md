# sportsedge: NFL and NBA picks, props and parlays

`sportsedge` predicts NFL and NBA games and player props from free public data. It compares each
prediction with the sportsbook's price, then ranks single bets and parlays by **expected value**. It also
lists the picks that are **most likely to win**.

```powershell
py -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\sportsedge ui                     # the dashboard in your browser
.\sportsedge share                  # the dashboard, reachable from anywhere
.\sportsedge slate                  # today's NBA + this week's NFL, in the terminal
.\sportsedge slate --sport nba --bankroll 500
.\sportsedge grade                  # settle past picks, show the real track record
.\sportsedge backtest               # how it would have done vs real closing lines
.\sportsedge factors --sport nfl    # what the model learned
```

`.\sportsedge` is a small launcher in the project folder that runs the tool with the project's own
Python (the `.venv` folder). It is short for `.\.venv\Scripts\python -m sportsedge`. Plain `python -m
sportsedge` uses your system Python, which does not have the libraries installed.

Each `slate` run prints to the terminal. It also writes an HTML report and a CSV of every priced bet
to `output/`. The first run downloads several seasons of data and tests the prop simulators on past
lines, which takes a few minutes. Later runs use the cache in `.cache/`.

## The dashboard

`.\sportsedge ui` starts a local server at http://127.0.0.1:8765 and opens it in your browser.
Only this computer can reach it. It runs the model in the background, and **Refresh** re-downloads
odds, injuries and weather and reruns everything. While it is open it also looks at the odds feed every
3 minutes, logs any change, and reruns the picks by itself when a line has moved.

| Page | What's there |
|---|---|
| **Today** | Value bets and most-likely picks as cards (each with its win chance as a ring), the best parlays by day, group of games and game, then every game as one block laid out like a sportsbook: the two teams with moneyline, over/under and spread, how far the line has moved since it opened, the game's **top picks**, and its player props underneath, with chips to switch stat. Games that have started sit in their own block with the live score, so a bet already placed can still be recorded. |
| **Player props** | Every prop line, **by game** (each game a section, grouped by stat or by player) or as a **ranked list** across all games (most likely, value, biggest disagreements). Also the test results for the prop simulator, a form to price a line from your own sportsbook, and the full projection table. |
| **Live** | Every bet you recorded as it stands right now, pick by pick, with real scores and stat lines, what the bet is worth, and a check on your sportsbook's cash-out offer. Below that, today's scoreboard: every game's score and clock, ESPN's live win chance for games in play, every player's stats so far, and the sportsbook's live prices. A bet with one pick left gets a hedge worked out. |
| **Line moves** | How each game's spread, total and moneyline have moved since the sportsbook first posted them: a chart per number for the game you pick, every game ranked by how far it has moved, and the player prop lines that moved most. Also whether the line for each pick in your recorded bets has moved in your favour or against you. |
| **Results** | How logged picks have done: units won over time, a chart of predicted vs actual win rates, results by list and bet type, closing-line value, and **My bets**. |
| **How it works** | The steps behind a pick, the game model's record against closing lines, the prop simulator's record against real lines, and every factor's learned weight. |

Every pick carries a rating: **Value** (positive expected return and a real edge), **Fair**, **Poor
price**, or **Check news** (the model is far from the sportsbook, which usually means news it has not
seen). The **? Guide** button explains every number in plain language.

**Bet slip** (right side, or the button at the bottom on a phone):
- Prices any combination you build: combined odds, the chance to win, fair odds, and expected return.
- Legs from the same game use the correlation measured in the simulation.
- **Odds on your sportsbook's slip**: build the same bet at your sportsbook and copy the odds it shows
  for the whole bet. The box starts at the standard price worked out from each pick's own odds. The slip
  then says plainly whether the bet is worth it, with a quarter-Kelly stake.
- **Record bet** saves it under My bets, where it's settled automatically. Place the actual bet at your sportsbook.

**If a line looks wrong.** Lines reach the tool through ESPN's feed of DraftKings prices, which can
run behind the sportsbook. Check the line against your sportsbook before betting.
- For a game, open **Different at your sportsbook? Correct the line** on its card and type the
  current spread, total or odds. The game is re-priced as if the feed had delivered those numbers. The
  correction is saved in `history/line_corrections.json` and kept, across restarts, until the feed changes.
- For a prop, use **Price a line from your sportsbook** and say where the line is from. A DraftKings
  line that moved replaces the tool's copy and is priced on its own odds. A line from another
  sportsbook is compared with the DraftKings line, so a line or price that is out of step shows as an edge.

## Top picks and suggested parlays

Each game lists its **top picks**: one side of each bet, one pick per player, best expected return for the
price first. The **Parlays** section then gives the best parlay for each **day**, each **group of games**
(one sport's games starting within 90 minutes of each other) and each **game**, at three sizes of payout:
Safer (about +100 to +300), Balanced (+300 to +800) and Long shot (+800 and up).

- **What "best" means.** A parlay priced from its picks' own odds returns the product of what each pick
  returns. So for a payout of a given size, the best parlay is the one whose picks give up the least to the
  sportsbook (or gain the most). That is how each band is filled, which gets the most odds the picks can
  justify. Choosing by payout alone would just choose the longest odds on the board.
- **Across games, no more than two picks come from one game**, so one game cannot sink the bet alone.
- **Fair odds are the number to check.** Every added pick lowers the chance to hit. Bet a parlay when your
  slip pays more than its fair odds.
- **The payout is marked as an estimate in two cases.** Picks from the same game get their own price from
  the sportsbook, usually below the separate-pick price when the picks tend to win together. And NFL props
  reach the tool without prices: they are shown as a **lean**, never as value, at an assumed standard price
  (`ASSUMED_PROP_ODDS`, −115) until you type in the odds from your slip. Their chance is the tested blend:
  the simulator against an even bet, at the weight the prop test gave the simulator.

## Recording a bet after the game has started

The slate only holds games still to come, because a started game's prices are no longer on offer. A game
that has started moves to **Started games** on the Today page, with the live score, each player's number
so far, and every pick at the last line and price logged before the start. Tap the picks you bet, type the
odds from your sportsbook's slip, change a line in the slip if yours was different, and record it. The Live
page then follows the bet like any other.

## Trading apps (prices in cents)

A trading app sells a contract that pays $1 if you are right, for a price in cents, and charges a fee on
top. The **−110 / 52¢** switch at the top (or **Guide and settings** on a phone) changes the whole tool to
that way of writing prices.

- **Shown:** every price appears in cents (−110 becomes 52¢, +150 becomes 40¢). Fair odds become a fair
  price. Each pick also shows **worth up to**: the most it is worth paying on your app once the fee is
  counted, which is the tool's win chance less the fee.
- **Typed:** the bet slip, the hedge box, the form for pricing a prop line and the form for correcting a
  game's line all take a price in cents. The fee is added before anything is worked out, so a return is
  what you would really get. The slip also shows what one contract costs with the fee and how many a
  given spend buys.
- **The fee** is set under Guide and settings as a flat amount per contract in cents, plus a percentage
  of price × (1 − price). Both start at zero: set them from your app's fee page, or every return will
  look better than it is.
- **What does not change.** The tool's own lines and prices still come from a sportsbook's feed, shown in
  cents for comparison. A trading app's line and price can differ, so type yours in. The settings are kept
  in the browser, so each device has its own. Inside the tool everything is still American odds: cents
  are converted where a price is shown and where one is typed.
- **Selling early** is the same question as a cash-out: type what you would get for selling into the
  cash-out box on the Live page.
- **A trade slip's percent is the price.** DraftKings' trade slip (the app in states without its
  sportsbook) writes a contract's price as a percent: 52% there is 52¢ here. The tool keeps ¢ for prices so
  they are not mistaken for its own win chances.
- **Type the payout when you can.** A trade slip shows what your spend pays (for example $10 to pay $18).
  The bet slip has a box for it in cents mode. Spend over payout is the real cost of a contract with the
  fee already in it, so the price box and the fee setting are then not used. Without a payout, only whole
  contracts are bought, as on the app, so a few cents of the spend can be left over.

## Pasting a bet slip

**Paste a slip**, at the top of the bet slip, reads a bet copied as text from your sportsbook (the slip or
My Bets). It finds the picks, the odds and the stake and fills the bet slip in, ready to check and record.
- A pick may be on one line ("Jayson Tatum Over 25.5 Points") or spread over several. "25+" is read as over
  24.5. Spreads, totals and moneylines are matched by team nickname.
- A line that differs from the tool's is kept as **your line** and recorded as such.
- Anything that cannot be matched to a pick the tool has (a stat it does not carry, a first-basket bet) is
  listed as not matched. Nothing is guessed.
- **A trade slip** is understood too: the percent under a pick is its price, and "Payout: $18" what its
  stake pays. When every pick has its own payout they are separate single bets, and one button records
  them all, each at the odds its payout works out to. The account balance and the +$5 / +$10 buttons are
  not mistaken for stakes.
- **From a screenshot:** the tool reads text, not pictures, but a phone can pull the text out. Open the
  screenshot, choose *Copy text* (Google Lens on Android, Live Text on iPhone), and paste.
- **A bet made at a line other than the tool's** (an alternate line, or a live one) is recorded at your
  line. The tool's line is kept beside it, because the stored win chance belongs to that line: the Live
  page reads the pre-game expectation from the tool's line and works the chance out at yours.

## Betting during a game

The feed carries the sportsbook's in-game prices for games in play (spread, total, moneyline and
over/under props), and the Live page shows them on the scoreboard next to the pre-game numbers.

- **The tool does not suggest new live bets.** Its live estimate knows the score, the clock and the box
  score, and nothing else. The first time it was set against real in-game prices it disagreed with a third
  of them by a wide margin (for example 89% against the sportsbook's 50% on a spread in a blowout). A gap
  that size is the estimate being wrong: big leads shrink and starters sit. Showing those as bets would
  lose money.
- **It logs them instead.** Each new live price is written beside the tool's estimate to
  `history/live_log.csv`, so the estimate can be tested against results and given a weight, the way the
  pre-game model and the prop simulator were.
- **A hedge is arithmetic, so that is offered.** When a recorded bet has one pick left and its game is on,
  the bet's card shows how much to stake on the other side, at the live price, to finish with the same
  amount whichever way it goes. If the live line is not the line you bet, it also shows the stretch where
  both bets win (a middle) or both lose. The price is prefilled from the feed and can be typed over, since
  a live price moves faster than any feed.

## Line moves

The feed gives each line's **opening number**, so "moved since it opened" is known for every game and
prop straight away. Opening numbers can be posted long before a game, so big moves are common.
- On top of that the dashboard checks the feed every 3 minutes (`ODDS_CHECK_MINUTES`) and writes a row to
  `history/odds_log.csv` whenever a game's numbers change. The charts on the **Line moves** page are drawn
  from that log, so they start when the dashboard first runs and fill in from there.
- When a check finds a spread or total that differs from the one the picks were priced at, and the last
  full update is at least 12 minutes old (`AUTO_UPDATE_MINUTES`), the picks are reworked automatically.
- A spread is described by the team it moved toward: "−1.5 → −8.5, toward DAL" means DAL became a bigger
  favourite.
- **Your bets and the line.** For every bet you record, each pick is compared with the sportsbook's number
  for it now, on the Live and Line moves pages: **in your favour** (you hold a better number than is on
  offer now), **against you**, or **no real change**. A moved line is measured in points or the stat's own
  units. An unmoved line is judged on its price, as the change in the pick's chance with the fee removed.
  Once the game starts the comparison stays at the last number seen before it (`history/bet_lines.json`),
  which is the bet's closing-line value as near to the close as the dashboard looked.

## Live tracking and cashing out

Record a bet from the bet slip and the **Live** page follows it. It checks for new numbers every 30
seconds while a game is on.
- Each pick shows the score and clock, the player's current stat against the line ("18 of 24.5, needs
  7 more"), and whether it is already decided. An over that has been reached is won before the game ends.
- A pushed or void pick drops out of a parlay and the payout shrinks, as at a sportsbook.
- **Worth right now** is what the bet would pay multiplied by the chance the remaining picks win.

**Thinking of cashing out?** Type the offer your sportsbook shows. The page compares it with what the
bet is worth and says hold or cash out.
- Sportsbooks usually offer less than a bet is worth and keep the difference. The page shows that cut.
- A sure amount is worth more than a gamble with the same average, more so when the possible win is
  large next to your bankroll. With a bankroll set, the page shows the amount at or above which cashing
  out makes sense for you. It uses the same risk setting that sizes stakes (quarter Kelly).
- It also shows how low the chance of winning would have to be for the offer to be the better deal.

The scores and stats are real. The live chances are **rough estimates**, simpler than the pre-game models.
Each one starts, at the opening tip or kick-off, exactly at the pick's pre-game chance, and moves only
with the score, the clock and the stat line:
- moneylines follow ESPN's live win probability, shifted to start from the pre-game chance;
- spreads and totals assume the rest of the game is played at the pre-game pace, with less uncertainty
  as the clock runs down;
- props assume the player keeps producing at a blend of his pre-game expectation and tonight's pace.

They do not know about foul trouble, injuries during the game, blowouts or who has the ball.

## Reach it from anywhere

`.\sportsedge share` gives the dashboard a permanent `https://<name>.vercel.app` address you
can open on your phone. All the work still happens on this computer, so it has to stay on with that
command running. (The prediction engine needs about 630 MB of libraries and a long-running process,
which Vercel does not allow, so Vercel only provides the address.)

One-time setup:
1. `.\sportsedge passcode` and choose a passcode. Only a scrambled form of it is saved.
2. Make sure the two free helper tools are installed: `cloudflared` (`winget install Cloudflare.cloudflared`)
   and an up-to-date Vercel CLI signed in to your account (`npm i -g vercel@latest`, then `vercel login`).

Then each time: `.\sportsedge share`, and open the address it prints.
- It starts the dashboard if needed, opens a secure outbound tunnel, points your Vercel address at it,
  and keeps the computer awake. If the tunnel drops it reconnects by itself.
- The address stays the same every time. When the command is not running it shows an error page.
- Anything arriving from outside needs the passcode. Sessions last 30 days per device, eight wrong tries
  lock that visitor out for 15 minutes, and until a passcode is set the address shows only a "locked"
  page. Browsers on this computer are never asked.
- `.\sportsedge passcode` again changes it and signs every device out.
  `.\sportsedge passcode --clear` locks remote access.

## What goes into a prediction

**NFL games**
- Opponent-adjusted team ratings, recalculated every week with recent games weighted more heavily:
  points, EPA/play, passing EPA per dropback, rushing EPA per carry, yards per attempt and per carry,
  sack rate, turnover rate, explosive-play rate, pace (plays per game), and pass rate.
- Starting-QB quality: EPA/play, shrunk toward replacement level when the sample is small. If the
  usual starter is listed Out or Doubtful, the backup's numbers are used.
- Injuries: the expected snap share of players listed Out or Doubtful, split into skill players,
  offensive line and defense.
- Rest (byes and short weeks), travel distance, time-zone change, neutral site, and division games.
- Weather: temperature, wind, precipitation and snow at the stadium (Open-Meteo forecast and history),
  and whether the game is played indoors.

**NBA games**
- Opponent-adjusted ratings: points, offensive rating, pace, eFG%, turnover %, offensive-rebound %,
  free-throw rate, and 3-point attempt rate.
- Rest days, back-to-backs, travel since the last game, time-zone change, and altitude (Denver, Utah).
- Missing players: expected minutes multiplied by per-minute impact for every rotation player who is
  out. Training uses box-score absences. Live games use the ESPN injury report.

**Player props**: the prop models are Monte Carlo simulations built on the game simulation.
- NFL: team plays, then the pass/rush split (adjusted for game script), then **target share, carry share
  and touch share**, then catch rate and yards per catch or carry. Opponent yards allowed to the
  position, wind and rain also enter. Touchdowns come from simulated team points.
- NFL roles: a player's share is his average over the games he played, and a team's shares are
  rescaled to add up to 100%. Sportsbooks post lines only for players with a role, so on a team with
  lines posted, a player without one is held to a small share. That stops a fill-in from keeping the
  starter's workload on paper after the starter returns.
- NBA: projected minutes (rescaled when teammates are out), then the player's per-minute share of team
  points, rebounds, assists and threes, applied to the simulated team output. Blowout minutes are
  included. A prop is void when the player sits, so each player's numbers use his minutes *if he plays*.
- Teammates share one simulated game, so props are correlated the way they are in real games. Parlays
  need that.

## How "unbiased" is handled

- **No hand-set weights.** Every factor weight is fitted by regression. Prop dispersion, game-script and
  weather effects are estimated from data.
- **No look-ahead.** Features for a game use only games played before it. The backtest is walk-forward:
  each season is predicted by a model trained only on earlier seasons.
- **The market is treated as evidence.** Closing lines are very accurate. The tool fits how much weight
  the model deserves against the line on out-of-sample games, and keeps that weight only if it also
  lowers error in **leave-one-season-out cross-validation**. Otherwise the weight is 0 and the tool
  defers to the market. The model's raw number, the market line, and the blend all appear side by side.
- **Probabilities are anchored to the market.** A game-line probability starts from the no-vig price.
  The model then moves it only by its point difference from the line, priced through a learned score
  distribution. That distribution comes from what happened in past games with similar lines, so key
  numbers are included: pushes on 3 and 7 are about 9% and 6%, and favourites of 10 or more win as
  often as history says.
- **Props are centred on the market.** Across a whole slate the prop simulator is shifted so that, on
  average, it agrees with the sportsbook for each stat. What is left is its opinion about one player
  relative to the others, which is the only part that can carry an edge.
- **No vig in comparisons.** Market probabilities use the power method to remove the vig, so edges
  are measured against fair prices.
- **Long shots need a bigger edge.** Backtests show moneylines longer than +250 are where win probabilities
  are least precise, so those must clear a 5-point edge instead of 2 points.
- **Big disagreements are flagged, not sold.** When the model is far from the sportsbook (a prop more
  than 30 points of probability away), the pick is labelled Check news and never listed as value.

### What the backtest found (walk-forward, real closing lines)

| | NFL (2,390 games, 2018-26) | NBA (4,185 games, 2023-27) |
|---|---|---|
| Margin error: model vs closing line | 10.08 vs 9.80 pts | 11.06 vs 10.76 pts |
| Total error: model vs closing line | 10.57 vs 10.42 pts | 14.84 vs 14.33 pts |
| Model weight that survives cross-validation | sides 0%, totals 18% | sides 0%, totals 0% |
| Live win-probability log loss vs no-vig closing ML | 0.6088 vs 0.6087 | 0.5797 vs 0.5797 |
| Replay of model-only bets at closing prices | spreads -2.5%, totals -0.4% ROI | spreads -3.4%, totals -4.9% ROI |

In short, the game model doesn't beat closing lines on sides in either sport, and the tool says so
instead of inventing edges. NFL totals are the one game market where the model shows a small signal.
Its totals bets lost only 0.4% over 1,931 bets, where random betting at -110 loses about 4.5%. Lines
earlier in the week are softer than closing lines.

### What the prop test found (real sportsbook lines)

For each past game the prop simulator is run as it would have been that morning, then compared with
the DraftKings line ESPN published for that game and with what the player actually did
(`propcheck.py`). It reruns automatically for each new game day.

| | NBA | NFL |
|---|---|---|
| Real lines tested | 23,186 closing lines **with prices** (30 game days) | 5,028 lines, **no prices** (8 weeks) |
| Simulator weight that survives leave-one-day-out testing | 10% | 25% (against the line as an even bet) |
| Simulator's strong leans (15 to 30 points from the book) won | 50.4% of 3,378 (book implied 49.3%) | 56.0% of 989 (yardage stats only: 53.6%) |
| Needed to beat the book's fee | 52.4% at -110 | 52.4% to 54.5% at -110 to -120 |
| Betting every prop blind | -6.4% (the book's fee) | not measurable without prices |

- **NBA:** the sportsbook's closing prop prices predicted outcomes better than the simulator, so a
  prop's win chance is 90% the book's own price. Expect few or no value props at DraftKings prices.
  Value is most likely when another sportsbook's line is out of step with DraftKings.
- **NFL:** the simulator's side of real lines won more often, which is encouraging, but prices were
  not available, so this is not proof of profit. On receptions and touchdowns one side often costs
  much more than the other. The replay is not told who sat out, which is slightly harder than real use.
- Building the test fixed two real biases. The NBA simulator had been discounting bench minutes by how
  often a player appears, which is wrong for a prop (projected points for 15+ minute players went from
  0.3 low to dead on). The NFL simulator had been giving out more than 100% of a team's targets and
  carries (receiving yards were projected 27% too high).

## Tracking results

Every `slate` run logs each priced pick and each listed parlay to `history/picks.csv` and
`history/parlays.csv`. Each bet is stored once; if you rerun before the game, the latest price
replaces the earlier one.
- `grade` settles finished games using official results: nflverse scores and box scores, and
  stats.nba.com game logs. A prop on a player who didn't play is void, as at a sportsbook. `slate`
  also grades automatically before it prices a new slate.
- `grade` records **closing-line value**: whether the number you were shown beat the sportsbook's
  last number before the game. That is the earliest reliable sign of a real edge, well before
  win-loss records mean anything.
  - Game lines: points for spreads and totals, no-vig probability for moneylines.
  - Props: compared with the closing prop line from the same feed. If the line moved, a lower closing
    line is good for an under and a higher one for an over. If the line is the same, the pick is judged
    on whether its side's no-vig price shortened. Results shows how many props beat the close.
- The report breaks results down by list (value bets, most-likely picks, all game lines, all
  props) and by market. It shows ROI with a 95% range, a calibration table (do 60% picks win 60%
  of the time?), and log loss for the tool, the no-vig market and the raw prop simulator.
- **The prop weight keeps learning from your results.** It starts at the weight from the prop test
  above. Once 150 of your own props are graded, the weight that best fits them is blended in.
- Use `--no-track` when testing with made-up lines (like `examples/props_example.csv`) so they don't
  pollute the history.
- `python -m unittest discover tests` runs the settlement, prop, live and passcode tests.

## Prop lines

Game lines (moneyline, spread, total) and prop lines come from ESPN's public feed of DraftKings prices
and need no key.
- **NBA props** arrive with lines and prices, so they are fully priced.
- **NFL props** arrive with lines only. The board shows the simulator's projection and lean for each;
  type in your sportsbook's odds to get an expected return.
- Optional: a free key from [the-odds-api.com](https://the-odds-api.com) (500 credits/month) in `.env` as
  `ODDS_API_KEY=...` adds priced NFL props. Each game costs about 10 credits per refresh.
- Optional: `--props-csv my_lines.csv` with columns `sport,player,market,line,over_odds,under_odds`.
  `examples/props_example.csv` shows the format. Its lines are made up, for testing only.
  - NFL markets: `pass_yds pass_tds pass_cmp pass_att rush_yds rush_att rec_yds receptions rush_rec_yds anytime_td`
  - NBA markets: `pts reb ast fg3m stl blk pra pr pa ra`
- Your own lines take priority over The Odds API, which takes priority over the ESPN feed.
  Set `ESPN_PROPS = False` in `config.py` to turn the free feed off.

## Reading the output

| Column | Meaning |
|---|---|
| Win chance (Model %) | The tool's probability of winning. For bets that can push, it is the probability of winning given no push. |
| Book's chance (No-vig mkt %) | The sportsbook's probability with the vig removed |
| Edge | Win chance minus the book's chance, in percentage points |
| Expected return (EV/unit) | Expected profit per 1 unit staked (+0.05 = +5%) |
| Stake (Kelly) | Quarter-Kelly fraction of bankroll, capped at 3% |
| Fair odds (parlays) | The price at which a parlay breaks even under the model |
| Win together (correlation lift) | How much more often same-game legs hit together than if they were independent |

Parlay lists:
- **Best value**: every leg is +EV and each leg is from a different game.
- **Most likely to hit**: the most likely combinations that still pay +150 or more.
- **Same game**: legs that are positively correlated. Books reprice same-game parlays, so bet one
  only when the offered price is at or above the fair odds.

## Settings and code

`sportsedge/config.py` holds the settings: decay half-lives, Kelly fraction, parlay size, the minimum
edge to flag, and the seasons used.

| File | Role |
|---|---|
| `ratings.py` | Time-decayed, opponent-adjusted ridge ratings |
| `nfl.py`, `nba.py` | Data loading and game features |
| `gamemodel.py` | Margin and total models, market blend, learned score distributions |
| `nfl_props.py`, `nba_props.py` | Player-prop simulators |
| `propcheck.py` | Replays the prop simulators on real past lines and learns their weight |
| `markets.py`, `parlay.py` | Pricing, market centring, EV, Kelly, parlay search |
| `plays.py` | Top picks per game and the best parlays per game, group of games and day |
| `slipread.py` | Reads a bet slip pasted as text and matches its picks to the tool's |
| `sources.py` | ESPN (schedule, odds, prop lines, injuries), Open-Meteo, The Odds API |
| `tracker.py` | Logs picks and your bets, settles them, closing-line value, learned prop weight |
| `live.py` | Live scores and stat lines for open bets, live chances, cash-out check, today's scoreboard |
| `oddslog.py` | Line movement: opening numbers, the log of every change, movement since open |
| `betmoves.py` | Each recorded pick against the sportsbook's number for it now, kept up to the start of the game |
| `web.py`, `ui/` | Local dashboard: API server plus a plain HTML/JS page (no build step) |
| `access.py`, `share.py`, `remote/` | Passcode gate, and the tunnel plus Vercel address for reaching it from anywhere |
| `backtest.py`, `report.py`, `cli.py` | Evaluation and output |

Data sources: nflverse (`nflreadpy`), stats.nba.com (`nba_api`), ESPN public endpoints (odds, prop
lines, injuries, historical lines), Open-Meteo (weather), and optionally The Odds API.

Every prediction is a probability, not a guarantee. Sportsbooks keep a margin on every bet, and even a
good model loses often over short stretches. Track your results, and only stake money you can afford
to lose.
