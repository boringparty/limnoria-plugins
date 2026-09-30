"""
Picks: A localized, non-monetary prediction market plugin for Limnoria IRC bots.
"""

import supybot
import supybot.world as world

__version__ = "1.0.0"
__author__ = "Limnoria Developer"
__url__ = ""

from . import config
from . import plugin
from importlib import reload

reload(config)
reload(plugin)

Class = plugin.Class
configure = config.configure
