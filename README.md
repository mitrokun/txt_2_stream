# Text Reader



The component provides a  action (service) that initiates the synthesis of text from a file and the generation of streaming audio for the media player. When the player is stopped, the position is saved, and restarting it will resume at approximately the same point.

<img width="825" height="665" alt="image" src="https://github.com/user-attachments/assets/2db67fa6-9734-4e3c-be1d-310e93ca12a8" />




The restriction to the Wyoming protocol only was chosen intentionally, as it allows for precise position synchronization, control over the synthesis queue, and avoidance of unnecessary resource consumption.

Working with system abstractions is potentially possible, but not a stable solution (since different voices have different tempos).
