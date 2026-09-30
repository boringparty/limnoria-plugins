import supybot.conf as conf
import supybot.registry as registry

def configure(advanced):
    from supybot.questions import expect, anything, something, yn
    conf.registerPlugin('Picks', True)

Picks = conf.registerPlugin('Picks')

conf.registerChannelValue(Picks, 'startingPoints',
    registry.Integer(1000, """Determines the starting point balance given to a new user upon their first wager or interaction."""))

conf.registerChannelValue(Picks, 'timezone',
    registry.String('America/Vancouver', """Configures the timezone used for market open/close dates (e.g. America/Vancouver, UTC, US/Eastern)."""))

conf.registerChannelValue(Picks, 'minWager',
    registry.Integer(1, """Minimum allowed points for a single wager."""))

conf.registerChannelValue(Picks, 'maxWager',
    registry.Integer(1000000, """Maximum allowed points for a single wager (0 for unlimited)."""))

conf.registerGlobalValue(Picks, 'databasePath',
    registry.String('prediction_market.sqlite', """Filename/path relative to the bot's data directory for the SQLite database."""))
