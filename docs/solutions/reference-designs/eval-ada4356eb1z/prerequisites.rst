.. _eval-ada4356eb1z prerequisites:

Prerequisites
===============================================================================

EVAL-ADA4356EB1Z (ZedBoard)
-------------------------------------------------------------------------------

Hardware prerequisites
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. :adi:`EVAL-ADA4356EB1Z <ADA4356>` evaluation board
#. `ZedBoard <https://digilent.com/reference/programmable-logic/zedboard/start>`__
   FPGA carrier platform
#. 12 V power supply for the ZedBoard
#. Some way to interact with the ZedBoard:

   - Micro-USB cable for UART console
   - LAN cable (Ethernet) for SSH or IIO applications

#. Internet connection (without proxies makes things much easier)
   to update the scripts/binaries on the SD card.
#. An SD card with at least 16 GB of memory (in case you are using
   Linux).
#. Signal generator or current source for analog input signals.

Software prerequisites
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. :external+scopy:doc:`Scopy <index>` v2.0 or later (must contain
   the IIO plugin)
#. :git-iio-oscilloscope:`IIO Oscilloscope <releases+>`
#. UART terminal application (PuTTY/TeraTerm/Minicom), 115200 8N1

.. note::

   :adi:`ADI <>` does not offer FPGA carrier platforms for sale or
   loan; getting one yourself is the normal part of development or
   evaluation.
