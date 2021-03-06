"""Component for interacting with a Lutron RadioRA 2 system."""
import logging

from pylutron import Button, Lutron
import voluptuous as vol

from homeassistant.components import logbook
from homeassistant.const import ATTR_ID, CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.util import slugify


from threading import Lock, Timer

DOMAIN = "lutron_custom"

PLATFORMS = ["light", "cover", "switch", "scene", "binary_sensor"]

_LOGGER = logging.getLogger(__name__)

LUTRON_BUTTONS = "lutron_buttons"
LUTRON_CONTROLLER = "lutron_controller"
LUTRON_DEVICES = "lutron_devices"

# Attribute on events that indicates what action was taken with the button.
ATTR_ACTION = "action"
ATTR_FULL_ID = "full_id"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_USERNAME): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(hass, base_config):
    """Set up the Lutron integration."""
    hass.data[LUTRON_BUTTONS] = []
    hass.data[LUTRON_CONTROLLER] = None
    hass.data[LUTRON_DEVICES] = {
        "light": [],
        "cover": [],
        "switch": [],
        "scene": [],
        "binary_sensor": [],
    }

    config = base_config.get(DOMAIN)
    hass.data[LUTRON_CONTROLLER] = Lutron(
        config[CONF_HOST], config[CONF_USERNAME], config[CONF_PASSWORD]
    )

    hass.data[LUTRON_CONTROLLER].load_xml_db()
    hass.data[LUTRON_CONTROLLER].connect()
    _LOGGER.info("Connected to main repeater at %s", config[CONF_HOST])

    # Sort our devices into types
    for area in hass.data[LUTRON_CONTROLLER].areas:
        for output in area.outputs:
            if output.type == "SYSTEM_SHADE":
                hass.data[LUTRON_DEVICES]["cover"].append((area.name, output))
            elif output.is_dimmable:
                hass.data[LUTRON_DEVICES]["light"].append((area.name, output))
            else:
                hass.data[LUTRON_DEVICES]["switch"].append((area.name, output))
        for keypad in area.keypads:
            for button in keypad.buttons:
                # If the button has a function assigned to it, add it as a scene
                if button.name != "Unknown Button" and button.button_type in (
                    "SingleAction",
                    "Toggle",
                    "SingleSceneRaiseLower",
                    "MasterRaiseLower",
                ):
                    # Associate an LED with a button if there is one
                    led = next(
                        (led for led in keypad.leds if led.number == button.number),
                        None,
                    )
                    hass.data[LUTRON_DEVICES]["scene"].append(
                        (area.name, keypad.name, button, led)
                    )

                hass.data[LUTRON_BUTTONS].append(
                    LutronButton(hass, area.name, keypad, button)
                )
        if area.occupancy_group is not None:
            hass.data[LUTRON_DEVICES]["binary_sensor"].append(
                (area.name, area.occupancy_group)
            )

    for platform in PLATFORMS:
        discovery.load_platform(hass, platform, DOMAIN, {}, base_config)
    return True


class LutronDevice(Entity):
    """Representation of a Lutron device entity."""

    def __init__(self, area_name, lutron_device, controller):
        """Initialize the device."""
        self._lutron_device = lutron_device
        self._controller = controller
        self._area_name = area_name

    async def async_added_to_hass(self):
        """Register callbacks."""
        self.hass.async_add_executor_job(
            self._lutron_device.subscribe, self._update_callback, None
        )

    def _update_callback(self, _device, _context, _event, _params):
        """Run when invoked by pylutron when the device state changes."""
        self.schedule_update_ha_state()

    @property
    def name(self):
        """Return the name of the device."""
        return f"{self._area_name} {self._lutron_device.name}"

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def unique_id(self):
        """Return a unique ID."""
        # Temporary fix for https://github.com/thecynic/pylutron/issues/70
        if self._lutron_device.uuid is None:
            return None
        return f"{self._controller.guid}_{self._lutron_device.uuid}"


class LutronButton:
    """Representation of a button on a Lutron keypad.

    This is responsible for firing events as keypad buttons are pressed
    (and possibly released, depending on the button type). It is not
    represented as an entity; it simply fires events.
    """

    def __init__(self, hass, area_name, keypad, button):
        """Register callback for activity on the button."""
        name = f"{keypad.name}: {button.name}"
        if button.name == "Unknown Button":
            name += f" {button.number}"
        self._hass = hass
        self._has_release_event = (
            button.button_type is not None and "RaiseLower" in button.button_type
        )
        self._id = slugify(name)
        self._keypad = keypad
        self._area_name = area_name
        self._button_name = button.name
        self._button = button
        self._event = "lutron_event"
        self._full_id = slugify(f"{area_name} {name}")
        self.lock = Lock()
        self.timer = None

        button.subscribe(self.button_callback, None)

    def button_callback(self, button, context, event, params):
        """Fire an event about a button being pressed or released."""
        _LOGGER.info("Lutron Button %s event %s", self._full_id, str(event))

        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None

            if event == Button.Event.PRESSED:
                self.button_action("pressed")

                def super_long_press_func():
                    with self.lock:
                        self.timer = None
                        self.button_action("super_long_pressed")

                def long_press_func():
                    with self.lock:
                        self.button_action("long_pressed")
                        self.timer = Timer(1, super_long_press_func)
                        self.timer.start()

                self.timer = Timer(0.5, long_press_func)
                self.timer.start()
            if event == Button.Event.RELEASED:
                self.button_action("released")

    def button_action(self, action):
        data = {
            ATTR_ID: self._id, 
            ATTR_ACTION: action, 
            ATTR_FULL_ID: self._full_id,
            'area_name': self._area_name,
            'button_name': self._button_name

        }
        self._hass.bus.fire(self._event, data)
        logbook.log_entry(self._hass, action, str(data), DOMAIN)


